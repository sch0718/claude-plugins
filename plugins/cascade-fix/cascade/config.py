import ast
import json
import re
import tomllib
from dataclasses import dataclass, field, fields
from fnmatch import fnmatch
from pathlib import Path

DEFAULT_SENSITIVE_PATHS = ["**/auth*", "**/security*", "**/*delete*", "**/migrations/**"]
CANDIDATE_KEYS = ("tests", "full_tests", "build", "typecheck", "lint", "format")
LIST_KEYS = (*CANDIDATE_KEYS, "setup", "sensitive_paths")
DICT_KEYS = ("doc_snippet_check",)


class ConfigError(ValueError):
    pass


class ExitCodes:
    NO_VERIFY_CONFIG = 4
    PARSE_ERROR = 5
    NEEDS_USER = 6


@dataclass
class RepoConfig:
    tests: list[str] = field(default_factory=list)
    full_tests: list[str] = field(default_factory=list)
    build: list[str] = field(default_factory=list)
    typecheck: list[str] = field(default_factory=list)
    lint: list[str] = field(default_factory=list)
    format: list[str] = field(default_factory=list)
    setup: list[str] = field(default_factory=list)
    doc_snippet_check: dict[str, str] = field(default_factory=dict)
    sensitive_paths: list[str] = field(default_factory=lambda: list(DEFAULT_SENSITIVE_PATHS))


def repo_entry(repos: dict, root: Path):
    """repos.toml의 [repos] 표에서 root 항목을 찾는다. 키는 ~·심볼릭 링크 경로로 적어도 된다."""
    root = Path(root).resolve()
    return next((v for k, v in repos.items() if Path(k).expanduser().resolve() == root), None)


def has_repos(path: Path) -> bool:
    try:
        return bool(tomllib.loads(path.read_text(encoding="utf-8")).get("repos"))
    except tomllib.TOMLDecodeError:
        return True  # 잘못된 파일이면 그대로 골라 오류를 보인다
    except OSError:
        return False


def load_repo_config(root: Path, config_path: Path) -> RepoConfig | None:
    config_path = Path(config_path)
    if not config_path.is_file():
        return None
    with config_path.open("rb") as f:
        data = tomllib.load(f)
    entry = repo_entry(data.get("repos", {}), root)
    if entry is None:
        return None
    known = {f.name for f in fields(RepoConfig)}
    values = {k: v for k, v in entry.items() if k in known}
    for key, value in values.items():
        if key in LIST_KEYS and not (isinstance(value, list) and all(isinstance(v, str) for v in value)):
            raise ConfigError(f"{key}는 문자열 목록이어야 한다(예: {key} = [\"...\"]): {value!r}")
        if key in DICT_KEYS and not (isinstance(value, dict) and all(isinstance(v, str) for v in value.values())):
            raise ConfigError(f"{key}는 언어별 명령 표여야 한다: {value!r}")
    return RepoConfig(**values)


def is_sensitive_path(path: str, patterns: list[str]) -> bool:
    lowered = path.lower()
    candidates = (lowered, "/" + lowered)
    return any(fnmatch(c, pat.lower()) for pat in patterns for c in candidates)


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _base_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _unittest_only(text: str) -> bool | None:
    """파일의 테스트가 모두 unittest로 돌아가면 True, pytest만 모으는 테스트가 있으면 False, 테스트가 없으면 None.

    클래스 자신의 이름은 보지 않고 기반 클래스만 본다. 같은 파일 안의 기반 클래스는 따라가고, 다른 모듈에서
    가져온 기반 클래스는 이름이 *TestCase(unittest.TestCase, IsolatedAsyncioTestCase 등)일 때만 TestCase로 본다.
    그 밖에는 TestCase가 아닌 것으로 본다(pytest는 둘 다 돌린다).
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return False
    classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}

    def is_case(name: str, seen: frozenset[str] = frozenset()) -> bool:
        node = classes.get(name)
        if node is None or name in seen:
            return False
        return any(base_is_case(b, seen | {name}) for b in node.bases)

    def base_is_case(base: ast.expr, seen: frozenset[str]) -> bool:
        # 같은 파일의 클래스는 이름(ast.Name)으로만 가리킨다. unittest.TestCase 같은 속성 접근은 가져온 것이다
        if isinstance(base, ast.Name) and base.id in classes:
            return is_case(base.id, seen)
        return _base_name(base).endswith("TestCase")

    if any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test")
           for n in tree.body):
        return False
    tests = [n for n in classes if n.startswith("Test") or is_case(n)]
    if not tests:
        return None
    return all(is_case(n) for n in tests)


def _test_runner(files: list[Path]) -> str | None:
    """test_*.py 들을 돌릴 러너. 모든 테스트가 TestCase일 때만 unittest, 아니면 pytest(TestCase도 함께 돈다).

    unittest discover는 pytest 함수·일반 Test 클래스를 세지 않아, 고르면 그 실패가 검증에서 빠진다.
    pytest를 잘못 고르는 쪽은 pytest가 없을 때 검증이 실패로 드러나므로 애매하면 pytest다.
    """
    if not files:
        return None
    verdicts = [_unittest_only(_read_text(f) or "") for f in files]
    if any(v is False for v in verdicts) or not any(verdicts):
        return "pytest"
    return "unittest"


def detect_candidates(root: Path) -> dict[str, list[str]]:
    root = Path(root)
    found: dict[str, list[str]] = {k: [] for k in CANDIDATE_KEYS}

    def add(key: str, command: str) -> None:
        if command not in found[key]:
            found[key].append(command)

    text = _read_text(root / "package.json")
    if text is not None:
        try:
            scripts = json.loads(text).get("scripts", {})
        except (json.JSONDecodeError, AttributeError):
            scripts = {}
        if "test" in scripts:
            add("tests", "npm run test")
            add("full_tests", "npm run test")
        for name in ("build", "lint", "typecheck", "format"):
            if name in scripts:
                add(name, f"npm run {name}")

    text = _read_text(root / "pyproject.toml")
    if text is not None:
        try:
            tool = tomllib.loads(text).get("tool", {})
        except tomllib.TOMLDecodeError:
            tool = {}
        if "pytest" in tool or "pytest" in text:
            add("tests", "pytest {files}")
            add("full_tests", "pytest")
        if "ruff" in tool:
            add("lint", "ruff check .")
            add("format", "ruff format {files}")
        if "mypy" in tool:
            add("typecheck", "mypy .")

    text = _read_text(root / "Makefile")
    if text is not None:
        for target, key in (("test", "tests"), ("lint", "lint"), ("build", "build")):
            if re.search(rf"^{target}\s*:", text, re.MULTILINE):
                add(key, f"make {target}")
                if key == "tests":
                    add("full_tests", f"make {target}")

    if not found["tests"]:
        tests_dir = next((d for d in ("tests", "test") if (root / d).is_dir()), None)
        test_files = sorted((root / tests_dir).glob("test_*.py")) if tests_dir else []
        runner = _test_runner(test_files)
        if runner == "unittest":
            # {files}를 넣지 않는다: 파일 경로로 넘기면 discover -s 와 달리 tests_dir가 sys.path에 안 들어가
            # 테스트 안의 형제 모듈 import가 깨지고, 관련 테스트가 없으면 0건 실행(3.12부터 실패)이 된다
            add("tests", f"python3 -m unittest discover -s {tests_dir} -p 'test_*.py'")
            add("full_tests", f"python3 -m unittest discover -s {tests_dir} -p 'test_*.py'")
        elif runner == "pytest":
            # unittest discover는 pytest 함수 테스트를 세지 않는다(섞여 있으면 빠지고, 그것만 있으면
            # 0건이 3.11까지 성공으로 끝난다). 실패하는 코드가 검증을 통과하지 않게 pytest를 낸다
            add("tests", "pytest {files}")
            add("full_tests", "pytest")

    return found
