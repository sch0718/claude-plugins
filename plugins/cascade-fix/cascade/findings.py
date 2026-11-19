import json
import re
import subprocess
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

SEVERITY_MAP = {"P0": "Critical", "P1": "Critical", "P2": "Warning", "P3": "Suggestion"}
SEVERITIES = ("Critical", "Warning", "Suggestion")

PR_QUERY = (
    "query($owner:String!,$repo:String!,$number:Int!){ repository(owner:$owner,name:$repo){ "
    "pullRequest(number:$number){ reviewThreads(first:100){ pageInfo { hasNextPage } nodes { id isResolved isOutdated path line "
    "startLine comments(first:50){ pageInfo { hasNextPage } nodes { id databaseId author { login } body createdAt "
    "pullRequestReview { id state } } } } } } } }"
)

_DOC_HEADER = re.compile(r"^[\s#*]*(Critical|Warning|Suggestion)[\s*:]*$")
_DOC_ITEM = re.compile(r"^\d+\.\s+(.+?)\s*$")
_DOC_FIELD = re.compile(r"^\s*(?:[-*]\s*)?(위치|근거|제안)\s*:\s*(.*?)\s*$")
_CODE_START = re.compile(r"^\s*-\s*\[P\d+\]")
_CODE_ITEM = re.compile(r"^\s*-\s*\[(P\d+)\]\s*(.+)\s+—\s+(\S+|.+?:\d+(?:-\d+)?)\s*$")
_PR_LABEL = re.compile(r"^\W*(Critical|Warning|Suggestion)\b", re.I)
GH_TIMEOUT = 60
_LABEL = re.compile(r"^(Critical|Warning|Suggestion):\s*")
_LOCATION = re.compile(r"^(.*?):(\d+)(?:-\d+)?$")
_EVIDENCE = re.compile(r"\*\*확인한 내용(?:\*\*:|:\*\*)\s*(.*?)(?:\n\s*\n|\Z)", re.S)


class ParseError(Exception):
    def __init__(self, line_no, line):
        super().__init__(f"지적 형식을 해석할 수 없습니다 (줄 {line_no}): {line}")
        self.line_no = line_no
        self.line = line


@dataclass
class Finding:
    id: str
    source: str
    severity: str
    location: str
    body: str
    evidence: str
    thread_id: str | None = None
    outdated: bool = False
    carried_from: str | None = None
    carried_after: list[str] = field(default_factory=list)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})


def write_findings(path, items):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([f.to_dict() for f in items], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_findings(path):
    return [Finding.from_dict(d) for d in json.loads(Path(path).read_text(encoding="utf-8"))]


def _make_id(round_no, index):
    return f"r{round_no}-{index:02d}"


def _first_location(text):
    first = text.split(",")[0].strip().strip("`")
    return first


def _normalize_code_location(raw):
    m = _LOCATION.match(raw)
    return f"{m.group(1)}:{m.group(2)}" if m else raw


def parse_codex_doc(text, round_no):
    lines = text.splitlines()
    if not any(_DOC_HEADER.match(line) for line in lines):
        raise ParseError(1, lines[0] if lines else "")
    result = []
    severity = None
    i = 0
    while i < len(lines):
        line = lines[i]
        header = _DOC_HEADER.match(line)
        if header:
            severity = header.group(1)
            i += 1
            continue
        item = _DOC_ITEM.match(line) if severity else None
        if not item:
            i += 1
            continue
        item_no = i + 1
        title = item.group(1)
        i += 1
        block = []
        while i < len(lines) and (not lines[i].strip() or lines[i][0] in " \t" or _DOC_FIELD.match(lines[i])):
            block.append(lines[i])
            i += 1
        fields_found = {}
        last = None
        for raw in block:
            m = _DOC_FIELD.match(raw)
            if m:
                last = m.group(1)
                fields_found[last] = m.group(2)
            elif last and raw.strip():
                fields_found[last] += " " + raw.strip()
        if not fields_found.get("위치"):
            raise ParseError(item_no, line)
        body_lines = [title] + [raw.strip().lstrip("-* ").strip() if _DOC_FIELD.match(raw) else raw.strip() for raw in block if raw.strip()]
        result.append(Finding(
            id=_make_id(round_no, len(result) + 1),
            source="codex-doc",
            severity=severity,
            location=_first_location(fields_found["위치"]),
            body="\n".join(body_lines),
            evidence=fields_found.get("근거", ""),
        ))
    return result


def unparsed_code_items(text):
    return [line.strip() for line in text.splitlines() if _CODE_START.match(line) and not _CODE_ITEM.match(line)]


def parse_codex_code(text, round_no):
    lines = text.splitlines()
    seen = set()
    result = []
    i = 0
    while i < len(lines):
        m = _CODE_ITEM.match(lines[i])
        i += 1
        if not m:
            continue
        priority, title, raw_location = m.group(1), m.group(2).strip(), m.group(3)
        block = []
        while i < len(lines) and (not lines[i].strip() or lines[i][0] in " \t"):
            block.append(lines[i].strip())
            i += 1
        body = "\n".join(block).strip()
        label = _LABEL.match(body)
        severity = label.group(1) if label else SEVERITY_MAP.get(priority, "Warning")
        if label:
            body = body[label.end():]
        key = (severity, title, raw_location, body)
        if key in seen:
            continue
        seen.add(key)
        result.append(Finding(
            id=_make_id(round_no, len(result) + 1),
            source="codex-code",
            severity=severity,
            location=_normalize_code_location(raw_location),
            body=f"{title}\n{body}" if body else title,
            evidence=body,
        ))
    return result


def pr_severity(body):
    first_line = body.strip().splitlines()[0] if body.strip() else ""
    m = _PR_LABEL.match(first_line)
    return m.group(1).capitalize() if m else "Warning"


def _pr_evidence(body):
    m = _EVIDENCE.search(body)
    return m.group(1).strip() if m else ""


def _pr_location(thread):
    line = thread.get("line") or thread.get("startLine")
    return f"{thread.get('path') or '?'}:{line if line else '?'}"


def _is_pending(comment):
    review = comment.get("pullRequestReview") or {}
    return review.get("state") == "PENDING"


def _login(comment):
    return (comment.get("author") or {}).get("login") or ""


def _has_next_page(connection):
    return bool((connection.get("pageInfo") or {}).get("hasNextPage"))


def fetch_pr_findings(owner, repo, number, me, gh, round_no=1, warnings=None):
    try:
        proc = subprocess.run(
            [*gh, "api", "graphql", "-f", f"query={PR_QUERY}", "-f", f"owner={owner}", "-f", f"repo={repo}",
             "-F", f"number={number}"],
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"gh graphql 호출 시간 초과({GH_TIMEOUT}초)") from None
    if proc.returncode != 0:
        raise RuntimeError(f"gh graphql 호출 실패: {proc.stderr.strip()}")
    data = json.loads(proc.stdout)
    connection = data["data"]["repository"]["pullRequest"]["reviewThreads"]
    if _has_next_page(connection) and warnings is not None:
        warnings.append("경고: 리뷰 스레드가 100개를 넘는다. 앞의 100개만 읽었다")
    result = []
    for thread in connection["nodes"]:
        if thread.get("isResolved"):
            continue
        if _has_next_page(thread["comments"]) and warnings is not None:
            warnings.append(f"경고: 스레드 {thread['id']}의 코멘트가 50개를 넘는다. 앞의 50개만 읽었다")
        comments = thread["comments"]["nodes"]
        if not comments or _is_pending(comments[0]) or _login(comments[0]) == me:
            continue
        first = comments[0]
        body = first["body"]
        replies = [f"답글(@{_login(c)}): {c['body']}" for c in comments[1:] if not _is_pending(c)]
        result.append(Finding(
            id=_make_id(round_no, len(result) + 1),
            source="pr-comment",
            severity=pr_severity(body),
            location=_pr_location(thread),
            body="\n".join([body, *replies]),
            evidence=_pr_evidence(body),
            thread_id=thread["id"],
            outdated=bool(thread.get("isOutdated")),
        ))
    return result
