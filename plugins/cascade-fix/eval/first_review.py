import argparse
import json
import shlex
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_ROOT))
from cascade.cli import FIRST_REVIEW_KEYS  # noqa: E402
from cascade.cli_steps import USAGE_FILE  # noqa: E402
from cascade.findings import (_LOCATION, SEVERITIES, ParseError, parse_codex_code, parse_codex_doc,  # noqa: E402
                              unparsed_code_items)
from cascade.review import (REVIEW_JOB, REVIEW_META, REVIEWER_CLAUDE, REVIEWER_CODEX, REVIEWER_CURSOR,  # noqa: E402
                            label, load_text)
from score import (NO_RECORD, TOKEN_KEYS, UNMEASURED_COST, VERDICT_FACT, VERDICT_NO, VERDICT_UNREPRODUCIBLE,  # noqa: E402
                   money, ratio_text, read_optional, stored_number, usage_rows)

RUNNERS = (REVIEWER_CLAUDE, REVIEWER_CODEX, REVIEWER_CURSOR)
TRUTH_VALUES = (VERDICT_FACT, VERDICT_NO, VERDICT_UNREPRODUCIBLE)
MODE_DOC, MODE_CODE = "doc", "code"
NEAR_LINES = 5
MERGED_FILE, REVIEW_FILE = "merged.json", "review.md"
FINDINGS_INPUT_FILE, FOUND_BY_FILE = "findings-input.json", "found-by.json"
FINAL_SUFFIX = ".final"
CURSOR_TOKEN_KEYS = {"input": "inputTokens", "output": "outputTokens", "cache_read": "cacheReadTokens",
                     "cache_write": "cacheWriteTokens"}
INIT_SOURCES = {MODE_DOC: "claude-doc", MODE_CODE: "claude-code"}
FINDING_KEYS = ("runner", "id", "severity", "location", "body")
PARTIAL_RECORD = "부분 기록(일부 로그에 usage 없음)"
UNMEASURED_FORMAT = "미측정(형식 확인 필요: {reason})"


class FirstReviewError(Exception):
    pass


def read_json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FirstReviewError(f"파일을 읽지 못했다: {path} ({exc})") from None


def write_json(path: Path, data) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def relative_location(location: str, root: str | None) -> str:
    if not root:
        return location
    text = normalize_location(location)
    if not text.startswith("/"):
        return location
    for prefix in {root, str(Path(root).resolve())}:
        prefix = prefix.rstrip("/") + "/"
        if text.startswith(prefix):
            return text[len(prefix):]
    return location


def parse_reviewer(runner: str, text: str, mode: str, root: str | None = None) -> list[dict]:
    parser = parse_codex_code if runner == REVIEWER_CODEX and mode == MODE_CODE else parse_codex_doc
    try:
        parsed = parser(text, 1)
    except ParseError as exc:
        raise FirstReviewError(f"{label(runner)} 결과를 해석하지 못했다: {exc}") from None
    return [{"runner": runner, "id": f.id, "severity": f.severity, "location": relative_location(f.location, root), "body": f.body,
             "evidence": f.evidence, "found_by": [runner]} for f in parsed]


def normalize_location(location: str) -> str:
    return " ".join(location.replace("`", "").split())


def location_distance(a: str, b: str) -> int | None:
    a, b = normalize_location(a), normalize_location(b)
    if a == b:
        return 0
    ma, mb = _LOCATION.match(a), _LOCATION.match(b)
    if ma and mb and ma.group(1) == mb.group(1):
        return abs(int(ma.group(2)) - int(mb.group(2)))
    return None


def group_distance(group: list[dict], finding: dict) -> int | None:
    if any(member["runner"] == finding["runner"] for member in group):
        return None
    distances = [location_distance(member["location"], finding["location"]) for member in group]
    near = [d for d in distances if d is not None and d <= NEAR_LINES]
    return min(near) if near else None


def propose_groups(findings: list[dict]) -> list[list[dict]]:
    groups: list[list[dict]] = []
    for finding in findings:
        scored = [(group_distance(group, finding), index) for index, group in enumerate(groups)]
        scored = [item for item in scored if item[0] is not None]
        if scored:
            groups[min(scored)[1]].append(finding)
        else:
            groups.append([finding])
    return groups


def group_id(number: int) -> str:
    return f"g{number:02d}"


def found_by_of(members: list[dict]) -> list[str]:
    return [runner for runner in RUNNERS if any(m["runner"] == runner for m in members)]


def group_record(number: int, members: list[dict]) -> dict:
    return {"id": group_id(number), "found_by": found_by_of(members), "proposed": len(members) > 1,
            "members": [{"runner": m["runner"], "id": m["id"]} for m in members]}


def merge(claude_text: str | None, codex_text: str | None, cursor_text: str | None, mode: str, root: str | None = None) -> dict:
    texts = {REVIEWER_CLAUDE: claude_text, REVIEWER_CODEX: codex_text, REVIEWER_CURSOR: cursor_text}
    if all(text is None for text in texts.values()):
        raise FirstReviewError("검토 결과를 하나 이상 준다(--claude, --codex, --cursor)")
    findings = [f for runner in RUNNERS if texts[runner] is not None for f in parse_reviewer(runner, texts[runner], mode, root)]
    groups = [group_record(number, members) for number, members in enumerate(propose_groups(findings), 1)]
    return {"mode": mode, "confirmed": False, "reviewers": {runner: {"given": texts[runner] is not None, "meta": None, "usage": None}
                                                              for runner in RUNNERS},
            "findings": findings, "groups": groups}


def read_result_text(runner: str, path: Path) -> str:
    try:
        if runner == REVIEWER_CLAUDE:
            text, _ = load_text(path, REVIEW_JOB if path.suffix == ".json" else None)
        else:
            text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise FirstReviewError(f"{label(runner)} 결과를 읽지 못했다: {path} ({exc})") from None
    if not text.strip():
        raise FirstReviewError(f"{label(runner)} 결과가 비어 있다(호출 실패일 수 있다): {path}")
    return text


def read_meta(path: Path) -> dict | None:
    meta = read_optional(path.parent / REVIEW_META)
    return {key: meta[key] for key in FIRST_REVIEW_KEYS if key in meta} if isinstance(meta, dict) else None


def cursor_envelope(raw: str) -> dict | None:
    envelope = None
    for text in [raw, *raw.splitlines()]:
        try:
            data = json.loads(text)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("type") == "result":
            envelope = data
            if text is raw:
                break
    return envelope


def cursor_usage(path: Path) -> dict | None:
    try:
        envelope = cursor_envelope(Path(path).read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    usage = envelope.get("usage") if envelope else None
    if not isinstance(usage, dict):
        return None
    tokens = {key: usage.get(source) for key, source in CURSOR_TOKEN_KEYS.items()}
    return tokens if all(stored_number(value) is not None for value in tokens.values()) else None


def cursor_log_of(final: Path) -> Path | None:
    return final.with_name(final.name.removesuffix(FINAL_SUFFIX)) if final.name.endswith(FINAL_SUFFIX) else None


def merge_files(claude: Path | None, codex: Path | None, cursor: Path | None, mode: str, docs: list[str] | None = None,
                root: str | None = None) -> dict:
    paths = {REVIEWER_CLAUDE: claude, REVIEWER_CODEX: codex, REVIEWER_CURSOR: cursor}
    texts = {runner: read_result_text(runner, Path(path)) if path else None for runner, path in paths.items()}
    merged = merge(texts[REVIEWER_CLAUDE], texts[REVIEWER_CODEX], texts[REVIEWER_CURSOR], mode, root)
    for runner, path in paths.items():
        if path is None:
            continue
        path = Path(path)
        merged["reviewers"][runner]["path"] = str(path)
        merged["reviewers"][runner]["meta"] = read_meta(path)
    if claude:
        merged["reviewers"][REVIEWER_CLAUDE]["usage"] = read_optional(Path(claude).parent / USAGE_FILE)
    if cursor and (log := cursor_log_of(Path(cursor))):
        merged["reviewers"][REVIEWER_CURSOR]["usage"] = cursor_usage(log)
    if docs:
        merged["docs"] = list(docs)
    return merged


def malformed(reason: str) -> FirstReviewError:
    return FirstReviewError(f"merged.json 형식이 맞지 않는다: {reason}")


def index_findings(merged: dict) -> dict[tuple[str, str], dict]:
    findings = merged.get("findings")
    if not isinstance(findings, list):
        raise malformed("findings가 목록이 아니다")
    for index, finding in enumerate(findings, 1):
        if not isinstance(finding, dict) or any(not isinstance(finding.get(key), str) for key in FINDING_KEYS) \
                or finding["runner"] not in RUNNERS:
            raise malformed(f"findings {index}번째 지적에 {', '.join(FINDING_KEYS)}가 문자열로 없거나 runner가 {'|'.join(RUNNERS)}가 아니다")
    return {(f["runner"], f["id"]): f for f in findings}


def check_groups(merged: dict) -> list[dict]:
    """확정된 묶음 목록. 확정 표시가 없거나 묶음이 지적과 맞지 않으면 오류다."""
    if not isinstance(merged, dict):
        raise malformed("최상위가 객체가 아니다")
    if merged.get("confirmed") is not True:
        raise FirstReviewError('확정되지 않았다: 묶음을 확인·수정한 뒤 merged.json의 "confirmed"를 true로 바꾼다')
    findings, groups = index_findings(merged), merged.get("groups")
    if not isinstance(groups, list):
        raise malformed("groups가 목록이 아니다")
    ids, assigned = [], {}
    for index, group in enumerate(groups, 1):
        if not isinstance(group, dict):
            raise malformed(f"groups {index}번째가 객체가 아니다")
        gid, members = group.get("id"), group.get("members")
        if not isinstance(gid, str) or not gid:
            raise FirstReviewError(f"id 없음: {index}번째 묶음에 id를 적는다")
        if not members:
            raise FirstReviewError(f"빈 묶음: {gid}")
        if not isinstance(members, list) or any(not isinstance(m, dict) for m in members):
            raise malformed(f"{gid}의 members가 객체 목록이 아니다")
        ids.append(gid)
        for member in members:
            key = (member.get("runner"), member.get("id"))
            if key not in findings:
                raise FirstReviewError(f"없는 지적: {gid}의 {key[0]} {key[1]}")
            if key in assigned:
                raise FirstReviewError(f"중복 배정: {key[0]} {key[1]}이 {assigned[key]}와 {gid}에 모두 있다")
            assigned[key] = gid
    if len(set(ids)) != len(ids):
        raise FirstReviewError(f"id 겹침: {', '.join(sorted({gid for gid in ids if ids.count(gid) > 1}))}")
    missing = [f"{runner} {fid}" for runner, fid in findings if (runner, fid) not in assigned]
    if missing:
        raise FirstReviewError(f"미배정 지적이 있다(어느 묶음에도 없음): {', '.join(missing)}")
    return groups


def member_findings(group: dict, findings: dict) -> list[dict]:
    members = [findings[(m["runner"], m["id"])] for m in group["members"]]
    return sorted(members, key=lambda f: RUNNERS.index(f["runner"]))


def group_severity(members: list[dict]) -> str:
    return min((f["severity"] for f in members), key=lambda severity: SEVERITIES.index(severity) if severity in SEVERITIES else len(SEVERITIES))


def representative(members: list[dict]) -> dict:
    top = group_severity(members)
    return next(f for f in members if f["severity"] == top)


def confirm(merged: dict, out: Path) -> list[dict]:
    groups = check_groups(merged)
    findings = index_findings(merged)
    inputs, found_by = [], {}
    for group in groups:
        members = member_findings(group, findings)
        rep = representative(members)
        inputs.append({"id": group["id"], "severity": rep["severity"], "location": rep["location"], "body": rep["body"],
                       "evidence": rep.get("evidence", "")})
        found_by[group["id"]] = [{"runner": f["runner"], "id": f["id"]} for f in members]
    out = Path(out)
    write_json(out / FINDINGS_INPUT_FILE, inputs)
    write_json(out / FOUND_BY_FILE, found_by)
    return inputs


def load_truth(path: Path) -> dict[str, str]:
    truth, errors = {}, []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise FirstReviewError(f"정답 파일을 읽지 못했다: {path} ({exc})") from None
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
            gid, value = item["group"], item["truth"]
            if not isinstance(gid, str):
                raise TypeError(gid)
        except (ValueError, KeyError, TypeError):
            errors.append(f"{number}번째 줄을 읽지 못했다: {line}")
            continue
        if gid in truth:
            errors.append(f"{number}번째 줄의 묶음 {gid}가 앞 줄과 겹친다")
        truth[gid] = value
    if errors:
        raise FirstReviewError("정답 파일 오류: " + "; ".join(errors))
    return truth


def check_truth(truth: dict[str, str], groups: list[dict]) -> None:
    known = {group["id"] for group in groups}
    bad = [f"{gid}={value}" for gid, value in truth.items() if gid in known and value not in TRUTH_VALUES]
    unknown = sorted(gid for gid in truth if gid not in known)
    if bad:
        raise FirstReviewError(f"정답은 {'|'.join(TRUTH_VALUES)} 중 하나다: {', '.join(bad)}")
    if unknown:
        raise FirstReviewError(f"정답에 없는 묶음 id가 있다: {', '.join(unknown)}")
    missing = [group["id"] for group in groups if group["id"] not in truth]
    if missing:
        raise FirstReviewError(f"정답 줄이 없는 묶음이 있다: {', '.join(missing)}")


def reviewer_score(runner: str, groups: list[dict], findings: dict, truth: dict[str, str]) -> dict:
    mine = [(g, f) for g in groups for f in member_findings(g, findings) if f["runner"] == runner]
    facts = [g for g in groups if truth.get(g["id"]) == VERDICT_FACT]
    found = [g for g in facts if runner in found_by_of(member_findings(g, findings))]
    unique = [g for g in found if found_by_of(member_findings(g, findings)) == [runner]]
    severity_of = {g["id"]: group_severity(member_findings(g, findings)) for g in groups}
    by_severity = {}
    for severity in SEVERITIES:
        all_facts = [g for g in facts if severity_of[g["id"]] == severity]
        by_severity[severity] = {
            "precision": (sum(truth[g["id"]] == VERDICT_FACT for g, f in mine if f["severity"] == severity),
                          sum(f["severity"] == severity for g, f in mine)),
            "recall": (sum(g in found for g in all_facts), len(all_facts))}
    return {"findings": len(mine), "precision": (sum(truth[g["id"]] == VERDICT_FACT for g, f in mine), len(mine)),
            "recall": (len(found), len(facts)), "unique": len(unique), "by_severity": by_severity}


def sum_tokens(rows: list[dict]) -> dict:
    return {key: sum(stored_number(row.get(key)) or 0 for row in rows) for key in TOKEN_KEYS}


def claude_cost(usage) -> dict:
    if usage is None:
        return {"tokens": None, "cost": None, "note": NO_RECORD}
    rows = usage_rows(usage, "review")
    if not rows:
        return {"tokens": None, "cost": None, "note": UNMEASURED_FORMAT.format(reason="사용량 행이 없다")}
    costs = [stored_number(row.get("cost")) for row in rows]
    unmeasured = any(cost is None for cost in costs)
    return {"tokens": sum_tokens(rows), "cost": None if unmeasured else sum(costs),
            "note": f"{UNMEASURED_COST}: 비용이 없는 작업이 있다" if unmeasured else None}


def token_cost(tokens: dict | None) -> dict:
    if tokens == {}:
        return {"tokens": None, "cost": None, "note": PARTIAL_RECORD}
    return {"tokens": tokens, "cost": None, "note": None if tokens else NO_RECORD}


def reviewer_record(merged: dict, runner: str) -> dict:
    reviewers = merged.get("reviewers")
    record = reviewers.get(runner) if isinstance(reviewers, dict) else None
    return record if isinstance(record, dict) else {}


def reviewer_costs(merged: dict, usage_claude, usage_cli) -> dict:
    claude = usage_claude if usage_claude is not None else reviewer_record(merged, REVIEWER_CLAUDE).get("usage")
    cursor = usage_cli if usage_cli is not None else reviewer_record(merged, REVIEWER_CURSOR).get("usage")
    return {REVIEWER_CLAUDE: claude_cost(claude), REVIEWER_CODEX: token_cost(None), REVIEWER_CURSOR: token_cost(cursor)}


def score(merged: dict, truth: dict[str, str], usage_claude: dict | None = None, usage_cli: dict | None = None) -> dict:
    groups = check_groups(merged)
    check_truth(truth, groups)
    findings = index_findings(merged)
    costs = reviewer_costs(merged, usage_claude, usage_cli)
    given = [runner for runner in RUNNERS if reviewer_record(merged, runner).get("given", True)]
    return {runner: {**reviewer_score(runner, groups, findings, truth), "cost": costs[runner]} for runner in given}


def pct(pair: tuple[int, int]) -> str:
    hits, total = pair
    return f"{hits}/{total} ({ratio_text(hits / total)})" if total else f"{hits}/{total}"


def cost_cells(cost: dict) -> tuple[str, ...]:
    tokens = cost["tokens"]
    token_cells = tuple(str(tokens[key]) for key in TOKEN_KEYS) if tokens else (NO_RECORD,) * len(TOKEN_KEYS)
    return (*token_cells, money(cost["cost"]) if cost["cost"] is not None else cost["note"] or UNMEASURED_COST)


def table(header: tuple[str, ...], rows: list[tuple[str, ...]]) -> list[str]:
    return ["| " + " | ".join(header) + " |", "|" + "---|" * len(header),
            *("| " + " | ".join(cell.replace("|", "\\|") for cell in row) + " |" for row in rows)]


def render(result: dict) -> str:
    lines = ["## 검토자별 정밀도·재현율", "",
             *table(("검토자", "지적 수", "정밀도", "재현율", "단독 사실"),
                    [(label(r), str(v["findings"]), pct(v["precision"]), pct(v["recall"]), str(v["unique"]))
                     for r, v in result.items()]),
             "", "## 등급별(정밀도는 검토자가 붙인 등급, 재현율은 묶음의 가장 높은 등급)", "",
             *table(("검토자", "등급", "정밀도", "재현율"),
                    [(label(r), severity, pct(s["precision"]), pct(s["recall"])) for r, v in result.items()
                     for severity, s in v["by_severity"].items()]),
             "", "## 첫 검토 비용(저장된 값의 합, 다시 계산하지 않음)", "",
             *table(("검토자", "입력", "출력", "캐시 읽기", "캐시 쓰기", "비용"),
                    [(label(r), *cost_cells(v["cost"])) for r, v in result.items()])]
    return "\n".join(lines)


def title_of(finding: dict) -> str:
    return finding["body"].splitlines()[0] if finding["body"] else ""


def review_markdown(merged: dict, out: Path, init_command: str) -> str:
    findings = index_findings(merged)
    rows = []
    for group in merged["groups"]:
        members = member_findings(group, findings)
        rep = representative(members)
        rows.append((group["id"], "제안" if group["proposed"] else "단독", group_severity(members),
                     " · ".join(f"{label(f['runner'])} {f['id']}" for f in members), rep["location"], title_of(rep)))
    given = ", ".join(label(r) for r, v in merged["reviewers"].items() if v["given"])
    return "\n".join([
        "# 첫 검토 합치기", "", f"- 검토자: {given}", f"- 지적 {len(merged['findings'])}건, 묶음 후보 {len(merged['groups'])}개", "",
        "## 묶음 확정 방법", "",
        f"1. 아래 표를 보고 `{MERGED_FILE}`의 `groups`를 고친다. 같은 지적이면 `members`를 한 묶음으로 옮기고, 다르면 나눈다.",
        "   위치가 같거나 같은 파일 5줄 안이면 묶음 후보로 제안했을 뿐이다. 자동으로 확정하지 않는다. 모든 지적은 한 묶음에만 둔다.",
        '2. 다 고쳤으면 `"confirmed"`를 `true`로 바꾼다.',
        f"3. 확정한다: `python3 eval/first_review.py merge --confirm {shlex.quote(str(out / MERGED_FILE))} --out {shlex.quote(str(out))}`",
        f"4. cascade-fix에 넣는다: `{init_command}`", "",
        "## 묶음 후보", "", *table(("묶음", "구분", "등급", "찾은 검토자(지적 id)", "위치", "제목"), rows), ""])


def init_command(mode: str, out: Path, docs: list[str]) -> str:
    parts = ["PYTHONPATH=<스킬 경로>", "python3", "-m", "cascade.cli", "init", "--source", INIT_SOURCES[mode], "--findings-json",
             shlex.quote(str(Path(out) / FINDINGS_INPUT_FILE)), *(f"--doc {shlex.quote(doc)}" for doc in docs)]
    return " ".join(parts)


def run_merge(args) -> int:
    mode = MODE_DOC if args.doc else MODE_CODE
    out = Path(args.out)
    if isinstance(previous := read_optional(out / MERGED_FILE), dict) and previous.get("confirmed") is True:
        raise FirstReviewError(f"확정된 {out / MERGED_FILE}가 이미 있어 덮어쓰지 않는다. 다시 합치려면 그 파일을 옮기거나 지운다")
    merged = merge_files(args.claude, args.codex, args.cursor, mode, args.doc, args.root)
    if mode == MODE_CODE and args.codex and (skipped := unparsed_code_items(read_result_text(REVIEWER_CODEX, Path(args.codex)))):
        print(f"경고: 형식을 읽지 못한 Codex 항목 {len(skipped)}건: {' / '.join(skipped)}", file=sys.stderr)
    write_json(out / MERGED_FILE, merged)
    command = init_command(mode, out, args.doc or [])
    (out / REVIEW_FILE).write_text(review_markdown(merged, out, command), encoding="utf-8")
    proposed = sum(g["proposed"] for g in merged["groups"])
    print(f"지적 {len(merged['findings'])}건 → 묶음 후보 {len(merged['groups'])}개(합치자고 제안한 것 {proposed}개)")
    print(f"묶음을 확정·수정할 파일: {out / REVIEW_FILE}, {out / MERGED_FILE}")
    print(f"확정: python3 eval/first_review.py merge --confirm {out / MERGED_FILE} --out {out}")
    return 0


def run_confirm(args) -> int:
    merged = read_json(Path(args.confirm))
    inputs = confirm(merged, Path(args.out))
    out = Path(args.out)
    print(f"묶음 {len(inputs)}개를 확정했다: {out / FINDINGS_INPUT_FILE}, {out / FOUND_BY_FILE}")
    print(f"cascade-fix에 넣는다: {init_command(merged.get('mode', MODE_CODE), out, merged.get('docs') or [])}")
    return 0


def run_score(args) -> int:
    usage_claude = read_json(Path(args.usage_claude)) if args.usage_claude else None
    usage_cli = sum_cli_logs(args.usage_cli) if args.usage_cli else None
    result = score(read_json(Path(args.merged)), load_truth(Path(args.truth)), usage_claude, usage_cli)
    print(render(result))
    if args.json:
        write_json(Path(args.json), result)
    return 0


def sum_cli_logs(paths: list[str]) -> dict | None:
    """로그마다 usage가 있어야 합계를 낸다. 일부만 있으면 부분 합계를 전체처럼 보이지 않도록 {}(부분 기록)를 돌려준다."""
    found = [cursor_usage(Path(path)) for path in paths]
    if not any(found):
        return None
    if not all(found):
        missing = [path for path, tokens in zip(paths, found) if not tokens]
        print(f"경고: usage가 없는 로그가 있어 Cursor 토큰 합계를 내지 않는다: {', '.join(missing)}", file=sys.stderr)
        return {}
    return {key: sum(tokens[key] for tokens in found) for key in CURSOR_TOKEN_KEYS}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="첫 검토 세 결과(Claude·Codex·Cursor)를 합쳐 묶음 후보를 만들고, 확정한 묶음으로 검토자별 정밀도·재현율·비용을 낸다")
    sub = parser.add_subparsers(dest="command", required=True)
    merge_parser = sub.add_parser("merge", help="결과를 합쳐 묶음 후보를 만들거나(--confirm 없이), 확정한 묶음을 cascade-fix 입력으로 쓴다")
    merge_parser.add_argument("--claude", help="Claude 검토 결과(review.json 또는 본문 파일)")
    merge_parser.add_argument("--codex", help="Codex 검토 결과(.final)")
    merge_parser.add_argument("--cursor", help="Cursor 검토 결과(.final). 같은 폴더의 원본 로그에서 토큰을 읽는다")
    merge_parser.add_argument("--doc", action="append", help="검토한 문서(문서 모드). 여러 번 줄 수 있다. 없으면 코드 모드")
    merge_parser.add_argument("--root", help="검토한 저장소(워크트리) 루트. 이 아래를 가리키는 절대 경로 위치를 상대 경로로 맞춘다")
    merge_parser.add_argument("--confirm", help="사람이 고쳐 확정한 merged.json. 주면 findings-input.json과 found-by.json을 쓴다")
    merge_parser.add_argument("--out", required=True, help="결과를 쓸 폴더")
    score_parser = sub.add_parser("score", help="확정한 merged.json과 정답으로 검토자별 정밀도·재현율·비용을 낸다")
    score_parser.add_argument("--merged", required=True, help="확정한 merged.json")
    score_parser.add_argument("--truth", required=True, help='정답 jsonl. 줄마다 {"group": "g01", "truth": "사실|아님|재현 불가"}')
    score_parser.add_argument("--usage-claude", help="Claude 첫 검토의 claude-usage.json(없으면 merged.json에 담긴 것)")
    score_parser.add_argument("--usage-cli", nargs="+", help="Cursor 원본 로그(없으면 merged.json에 담긴 토큰)")
    score_parser.add_argument("--json", help="결과를 JSON으로 쓴다")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "score":
            return run_score(args)
        if args.confirm:
            if any((args.claude, args.codex, args.cursor, args.doc, args.root)):
                raise FirstReviewError("--confirm은 --claude·--codex·--cursor·--doc·--root와 함께 쓰지 않는다")
            return run_confirm(args)
        return run_merge(args)
    except (FirstReviewError, OSError) as exc:
        print(exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
