import json
import os
import subprocess
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from doclookup.config import Config, Extension
from doclookup.model import Ref, Unit

TIMEOUTS = {"signature-files": 10.0, "units": 60.0, "roots": 10.0, "cites": 60.0}
NO_TARGET = "대상 없음"


class ExtError(Exception):
    pass


@dataclass
class ExtData:
    units: list[Unit] = field(default_factory=list)
    roots: dict[str, Path] = field(default_factory=dict)
    cites: list[Ref] = field(default_factory=list)
    files: list[Path] = field(default_factory=list)


def _env() -> dict[str, str]:
    """실행기가 넣은 PYTHONPATH(플러그인 src)를 확장 프로세스에 물려주지 않는다."""
    return {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}


def _command(spec: Extension, sub: str, args: list[str]) -> list[str]:
    return [*spec.command, sub, *args]


def call(spec: Extension, sub: str, args: list[str], cwd: Path, timeout: float) -> dict:
    where = f"확장 {spec.name} {sub}"
    try:
        done = subprocess.run(_command(spec, sub, args), cwd=cwd, env=_env(), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ExtError(f"{where}: 시간 제한 {timeout:g}초를 넘었다") from None
    except OSError as err:
        raise ExtError(f"{where}: 실행할 수 없다 ({err.strerror or err})") from None
    if done.returncode != 0:
        tail = " ".join(done.stderr.strip().splitlines()[-3:])
        raise ExtError(f"{where}: 종료 코드 {done.returncode}" + (f" ({tail})" if tail else ""))
    try:
        data = json.loads(done.stdout)
    except json.JSONDecodeError:
        raise ExtError(f"{where}: 표준 출력이 JSON이 아니다") from None
    if not isinstance(data, dict):
        raise ExtError(f"{where}: JSON 객체가 아니다")
    return data


def run(spec: Extension, name: str, args: list[str], cwd: Path) -> int:
    """확장 명령의 표준 출력·종료 코드를 그대로 넘긴다. 시간 제한이 없다."""
    try:
        return subprocess.run(_command(spec, "run", [name, *args]), cwd=cwd, env=_env()).returncode
    except OSError as err:
        raise ExtError(f"확장 {spec.name} run {name}: 실행할 수 없다 ({err.strerror or err})") from None


def _field(spec: Extension, sub: str, data: dict, key: str, kind: type) -> object:
    value = data.get(key)
    if not isinstance(value, kind):
        raise ExtError(f"확장 {spec.name} {sub}: `{key}`가 {kind.__name__}이 아니다")
    return value


def _nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def _text(spec: Extension, sub: str, item: dict, key: str, required: bool = False) -> str | None:
    value = item.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value):
        raise ExtError(f"확장 {spec.name} {sub}: 항목의 `{key}`가 문자열이 아니다")
    return _nfc(value)


def _line(spec: Extension, sub: str, item: dict) -> int:
    value = item.get("line")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ExtError(f"확장 {spec.name} {sub}: 항목의 `line`이 1 이상의 정수가 아니다")
    return value


def _units(spec: Extension, data: dict) -> list[Unit]:
    units = []
    for item in _field(spec, "units", data, "units", list):
        if not isinstance(item, dict):
            raise ExtError(f"확장 {spec.name} units: 항목이 객체가 아니다")
        uid = _text(spec, "units", item, "uid", True)
        units.append(Unit(
            uid=uid, doc=_text(spec, "units", item, "doc") or uid.partition("#")[0],
            kind=_text(spec, "units", item, "kind") or "단위", number=_text(spec, "units", item, "number"),
            title=_text(spec, "units", item, "title"), path=_text(spec, "units", item, "path") or "",
            parent=_text(spec, "units", item, "parent"), file=_text(spec, "units", item, "file", True),
            line=_line(spec, "units", item), text=_text(spec, "units", item, "text") or "", slug=None,
            source=f"ext:{spec.name}", text_line=None,
        ))
    return units


def _cites(spec: Extension, data: dict) -> list[Ref]:
    cites = []
    for item in _field(spec, "cites", data, "cites", list):
        if not isinstance(item, dict):
            raise ExtError(f"확장 {spec.name} cites: 항목이 객체가 아니다")
        target = _text(spec, "cites", item, "target")
        cites.append(Ref(_text(spec, "cites", item, "file", True), _line(spec, "cites", item), None, "cite",
                         target or NO_TARGET, "", _text(spec, "cites", item, "raw") or "", not target))
    return cites


def _paths(spec: Extension, sub: str, values: object) -> list[str]:
    if not all(isinstance(v, str) and v for v in values):
        raise ExtError(f"확장 {spec.name} {sub}: 경로가 문자열이 아니다")
    return list(values)


def signature_files(cfg: Config, root: Path) -> list[Path]:
    files = []
    for spec in cfg.extensions:
        data = call(spec, "signature-files", [], root, TIMEOUTS["signature-files"])
        files += [Path(p) for p in _paths(spec, "signature-files", _field(spec, "signature-files", data, "files", list))]
    return files


def collect(cfg: Config, root: Path) -> ExtData:
    found = ExtData(files=signature_files(cfg, root))
    for spec in cfg.extensions:
        found.units += _units(spec, call(spec, "units", [], root, TIMEOUTS["units"]))
        roots = _field(spec, "roots", call(spec, "roots", [], root, TIMEOUTS["roots"]), "roots", dict)
        for prefix, folder in zip(_paths(spec, "roots", roots.keys()), _paths(spec, "roots", roots.values())):
            found.roots[_nfc(prefix)] = Path(folder)
        found.cites += _cites(spec, call(spec, "cites", [], root, TIMEOUTS["cites"]))
    return found
