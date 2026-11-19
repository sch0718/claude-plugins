"""Bundle for another device: export what is safe to carry, then apply it with path remapping."""
from __future__ import annotations

import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from session_move.app import app_is_running
from session_move.backlog import merge_backlog
from session_move.backup import backup
from session_move.company import CompanyRules, origin_url
from session_move.environment import Words, denylist_path, iter_files, read_jsonc, unit_status
from session_move.jsonio import load_json, write_json
from session_move.manifest import ITEMS, build_manifest, parse_items
from session_move.pathmap import (PathMap, remap_backlog_item, remap_schedule, remap_session, remap_settings,
                                  rewrite_jsonl)
from session_move.paths import Paths
from session_move.schedules import merge_schedules
from session_move.scopes import list_scopes
from session_move.secrets import blank_mcp, scan_secrets
from session_move.sessions import apply_sessions, plan_sessions


class ExportRefused(Exception):
    pass


@dataclass
class Report:
    included: Counter = field(default_factory=Counter)
    excluded: Counter = field(default_factory=Counter)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = list(self.warnings)
        if self.included:
            out.append("담은 것: " + ", ".join(f"{k} {v}" for k, v in sorted(self.included.items())))
        for k, v in sorted(self.excluded.items()):
            out.append(f"{k} {v}개 제외")
        out += self.notes
        return out


def agentmemory_running(home: Path) -> bool:
    fake = os.environ.get("SESSION_MOVE_FAKE_AGENTMEMORY_RUNNING")
    if fake is not None:
        return fake == "1"
    pid_file = home / ".agentmemory" / "worker.pid"
    if pid_file.is_file():
        try:
            os.kill(int(pid_file.read_text().strip()), 0)
            return True
        except (ValueError, OSError):
            pass
    try:
        with socket.create_connection(("127.0.0.1", 3111), timeout=0.3):
            return True
    except OSError:
        return False


def _transcript_paths(paths: Paths, cli_ids: list[str]) -> list[Path]:
    out = []
    for cli in cli_ids:
        if not cli:
            continue
        out += list(paths.projects_root.glob(f"*/{cli}.jsonl"))
        out += [p for p in paths.projects_root.glob(f"*/{cli}") if p.is_dir()]
    return out


class _Tar:
    def __init__(self, out: Path, report: Report):
        self.out = out
        self.report = report
        self.tar = tarfile.open(out, "w:gz")

    def add_bytes(self, name: str, data: bytes) -> None:
        info = tarfile.TarInfo(name)
        info.size = len(data)
        info.mode = 0o600
        self.tar.addfile(info, io.BytesIO(data))

    def add_json(self, name: str, value) -> None:
        self.add_bytes(name, json.dumps(value, ensure_ascii=False, indent=2).encode())

    def add_file(self, name: str, path: Path, check_secrets: bool = True) -> bool:
        if path.is_symlink():
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = os.readlink(path)
            self.tar.addfile(info)
            return True
        if check_secrets and scan_secrets(path):
            self.report.excluded["비밀값 의심 파일"] += 1
            return False
        self.tar.add(path, arcname=name, recursive=False)
        return True

    def add_tree(self, prefix: str, root: Path) -> None:
        for p in iter_files(root):
            self.add_file(f"{prefix}/{p.relative_to(root).as_posix()}", p)

    def close(self) -> None:
        self.tar.close()
        os.chmod(self.out, 0o600)


def _descendants(sessions: list[dict], roots: set[str]) -> set[str]:
    out = set(roots)
    changed = True
    while changed:
        changed = False
        for s in sessions:
            if s.get("parentSessionId") in out and s["sessionId"] not in out:
                out.add(s["sessionId"])
                changed = True
    return out


def export_bundle(paths: Paths, source: str, out: Path, items: set[str], include_company: bool,
                  no_company_check: bool) -> Report:
    report = Report()
    rules = CompanyRules.load(paths.company_config)
    words = None
    if not include_company:
        if rules is None and not no_company_check:
            raise ExportRefused(
                f"회사 목록 설정이 없거나 비어 있습니다: {paths.company_config}\n"
                "orgs·repos·path_prefixes 중 하나 이상을 적거나, 회사 판정 없이 만들려면 --no-company-check 를 주세요.")
        if rules is None:
            report.warnings.append("경고: 회사 판정 없이 만들었습니다(--no-company-check).")
        else:
            words, has_deny = Words.load(rules, denylist_path(paths.home))
            if not has_deny:
                report.warnings.append("경고: 금지어 목록 없음 — 회사 설정의 이름만으로 검사했습니다.")

    full = build_manifest(paths, source, set(ITEMS) | {"sessions"}, rules if words else None)
    sdir = paths.scope_dir(source)
    excluded_ids: set[str] = set()
    company_present = False
    if words:
        repo_hits = {s["sessionId"] for s in full["sessions"] if s["isCompany"]}
        text_hits = set()
        for s in full["sessions"]:
            if s["sessionId"] in repo_hits:
                continue
            files = [sdir / f"{s['sessionId']}.json"] + _transcript_paths(paths, [s["cliSessionId"], *s["priorCliSessionIds"]])
            if any(unit_status(f, words) == "hit" for f in files if f.exists()):
                text_hits.add(s["sessionId"])
        arch_hits = set()
        for a in full["archived"]:
            f = sdir / f"{a['sessionId']}.json"
            if a.get("isCompany") or (f.exists() and words.file_status(f) == "hit"):
                arch_hits.add(a["sessionId"])
        excluded_ids = _descendants(full["sessions"], repo_hits | text_hits) | arch_hits
        company_present = bool(excluded_ids)
        if repo_hits:
            report.excluded["회사 저장소 세션(하위 세션 포함)"] += len(_descendants(full["sessions"], repo_hits))
        if text_hits:
            report.excluded["회사 내용 세션"] += len(text_hits)
        if arch_hits:
            report.excluded["회사 보관 세션"] += len(arch_hits)

    def keep_item(value, cwd: str | None = None) -> bool:
        if not words:
            return True
        if cwd and rules and rules.is_company(cwd):
            return False
        return not words.hit_json(value)

    sessions = [s for s in full["sessions"] if s["sessionId"] not in excluded_ids]
    archived = [a for a in full["archived"] if a["sessionId"] not in excluded_ids]
    active_ids = {s["sessionId"] for s in sessions}
    groups = []
    for g in full["groups"]:
        if words and words.hit(g["name"]):
            report.excluded["회사 이름 그룹"] += 1
            continue
        groups.append({**g, "sessionIds": [i for i in g["sessionIds"] if i in active_ids]})
    sections = [s for s in full["sections"] if not (words and words.hit(s["name"]))]
    report.excluded["회사 이름 섹션"] += len(full["sections"]) - len(sections)
    presets = [p for p in full["presets"] if keep_item(p, (p.get("target") or {}).get("cwd"))]
    report.excluded["회사 프리셋"] += len(full["presets"]) - len(presets)

    schedules = []
    for t in full["schedules"]:
        body = Path(t.get("filePath") or paths.claude_dir / "scheduled-tasks" / t["id"] / "SKILL.md")
        if keep_item(t, t.get("cwd")) and not (words and body.exists() and words.file_status(body) != "clean"):
            schedules.append(t)
    report.excluded["회사 예약 작업"] += len(full["schedules"]) - len(schedules)
    backlog = [b for b in full["backlog"]
               if b.get("originSessionId") not in excluded_ids and keep_item(b, b.get("cwd"))]
    report.excluded["회사 backlog 항목"] += len(full["backlog"]) - len(backlog)
    report.excluded = Counter({k: v for k, v in report.excluded.items() if v})

    manifest = dict(full, items=sorted(items))
    manifest.update(
        sessions=sessions if {"sessions", "transcripts"} & items else [],
        archived=[{k: v for k, v in a.items() if k != "isCompany"} for a in archived] if "sessions" in items else [],
        groups=groups if "sidebar" in items else [],
        sections=sections if "sidebar" in items else [],
        pinnedSessionIds=[i for i in full["pinnedSessionIds"] if i in active_ids] if "sidebar" in items else [],
        presets=presets if "presets" in items else [],
        schedules=schedules if "schedules" in items else [],
        backlog=backlog if "backlog" in items else [],
    )
    for s in manifest["sessions"]:
        s.pop("isCompany", None)

    tar = _Tar(out, report)
    try:
        if "sessions" in items:
            for s in sessions:
                tar.add_file(f"sessions/{s['sessionId']}.json", sdir / f"{s['sessionId']}.json", check_secrets=False)
            tar.add_json("archived-ids.json", manifest["archived"])
            report.included["세션"] = len(sessions)
        folders: set[str] = set()
        if "transcripts" in items:
            n = 0
            for s in sessions:
                for p in _transcript_paths(paths, [s["cliSessionId"], *s["priorCliSessionIds"]]):
                    folders.add(p.parent.name)
                    if p.is_dir():
                        tar.add_tree(f"transcripts/{p.parent.name}/{p.name}", p)
                    elif tar.add_file(f"transcripts/{p.parent.name}/{p.name}", p):
                        n += 1
            report.included["대화 기록"] = n
        if "schedules" in items:
            tar.add_json("scheduled-tasks.json", {"scheduledTasks": schedules})
            for t in schedules:
                body = Path(t.get("filePath") or paths.claude_dir / "scheduled-tasks" / t["id"] / "SKILL.md")
                if body.is_file():
                    tar.add_file(f"scheduled-tasks/{t['id']}/SKILL.md", body)
            report.included["예약 작업"] = len(schedules)
        if "backlog" in items:
            src_backlog = load_json(sdir / "backlog" / "tasks.json", {}) or {}
            tar.add_json("backlog/tasks.json", {**src_backlog, "items": backlog})
        if "instructions" in items:
            _export_instructions(paths, tar, words, report)
        if "skills" in items:
            _export_skills(paths, tar, words, report)
        if "mcp" in items:
            _export_mcp(paths, tar, words, report)
        if "plugins" in items:
            _export_plugins(paths, tar, words)
        if "repos" in items:
            _export_repos(paths, tar, sessions, sdir, rules if words else None, words, report)
        if "memory" in items:
            _export_memory(paths, tar, sessions, words, company_present, include_company, report)
        tar.add_json("manifest.json", manifest)
    finally:
        tar.close()
    return report


def _export_instructions(paths: Paths, tar: _Tar, words: Words | None, report: Report) -> None:
    claude = paths.claude_dir
    md = claude / "CLAUDE.md"
    if md.is_file():
        tar.add_file("claude/CLAUDE.md", md)
        if words and words.file_status(md) == "hit":
            report.warnings.append("경고: 전역 CLAUDE.md에 회사 내용 포함(사용자 결정에 따라 담음).")
    st = claude / "settings.json"
    if st.is_file():
        if words and words.file_status(st) != "clean":
            report.excluded["회사 내용 settings.json"] += 1
        else:
            tar.add_file("claude/settings.json", st)
    cmds = claude / "commands"
    if cmds.is_dir():
        for p in sorted(iter_files(cmds)):
            if words and words.file_status(p) != "clean":
                report.excluded["회사 내용 명령"] += 1
                continue
            tar.add_file(f"claude/commands/{p.relative_to(cmds).as_posix()}", p)


def _export_skills(paths: Paths, tar: _Tar, words: Words | None, report: Report) -> None:
    excluded_agents: set[str] = set()
    agents = paths.home / ".agents" / "skills"
    if agents.is_dir():
        for unit in sorted(p for p in agents.iterdir() if p.is_dir() and not p.name.startswith(".")):
            status = unit_status(unit, words)
            if status != "clean":
                excluded_agents.add(str(unit))
                report.excluded["회사 내용 스킬" if status == "hit" else "검사할 수 없는(이진) 스킬"] += 1
                continue
            tar.add_tree(f"agents-skills/{unit.name}", unit)
    skills = paths.claude_dir / "skills"
    if skills.is_dir():
        for unit in sorted(p for p in skills.iterdir() if not p.name.startswith(".")):
            if unit.is_symlink():
                target = os.readlink(unit)
                resolved = str((unit.parent / target).resolve()) if not os.path.isabs(target) else target
                if resolved in excluded_agents or (words and words.hit(target)):
                    report.excluded["회사 내용 스킬"] += 1
                    continue
                tar.add_file(f"claude/skills/{unit.name}", unit)
                continue
            if not unit.is_dir():
                continue
            status = unit_status(unit, words)
            if status != "clean":
                report.excluded["회사 내용 스킬" if status == "hit" else "검사할 수 없는(이진) 스킬"] += 1
                continue
            tar.add_tree(f"claude/skills/{unit.name}", unit)


def _export_mcp(paths: Paths, tar: _Tar, words: Words | None, report: Report) -> None:
    servers = {}
    user_json = paths.home / ".claude.json"
    if user_json.is_file():
        try:
            servers.update((load_json(user_json, {}) or {}).get("mcpServers", {}))
        except ValueError:
            report.notes.append("~/.claude.json 을 읽지 못해 MCP 정의 일부를 건너뜀")
    mcp = paths.claude_dir / "mcp.json"
    if mcp.is_file():
        try:
            servers.update((read_jsonc(mcp) or {}).get("mcpServers", {}))
        except ValueError:
            report.notes.append("~/.claude/mcp.json 을 읽지 못해 건너뜀")
    kept = {}
    for name, spec in servers.items():
        if words and words.hit_json({name: spec}):
            report.excluded["회사 MCP 서버"] += 1
            continue
        kept[name] = spec
    tar.add_json("mcp.json", {"mcpServers": blank_mcp(kept)})


def _export_plugins(paths: Paths, tar: _Tar, words: Words | None) -> None:
    plugins_dir = paths.claude_dir / "plugins"
    known = load_json(plugins_dir / "known_marketplaces.json", {}) or {}
    installed = load_json(plugins_dir / "installed_plugins.json", {}) or {}
    markets = {k: v.get("source") for k, v in known.items() if not (words and words.hit_json({k: v.get("source")}))}
    names = sorted((installed.get("plugins") or {}).keys())
    tar.add_json("plugins.json", {"marketplaces": markets,
                                  "installed": [n for n in names if not (words and words.hit(n))]})


def _repo_root(cwd: str) -> Path | None:
    for d in [Path(cwd), *Path(cwd).parents]:
        if (d / ".git").exists():
            return d
    return None


def _export_repos(paths: Paths, tar: _Tar, sessions: list[dict], sdir: Path, rules, words, report: Report) -> None:
    repos = {}
    for s in sessions:
        rec = load_json(sdir / f"{s['sessionId']}.json", {}) or {}
        for cwd in (rec.get("originCwd"), rec.get("cwd")):
            root = _repo_root(cwd) if cwd else None
            if not root or str(root) in repos:
                continue
            url = origin_url(str(root))
            if not url or (rules and rules.is_company(str(root))) or (words and words.hit(url)):
                continue
            repos[str(root)] = {"path": str(root), "url": url, "branches": []}
        branch = rec.get("branch")
        root = _repo_root(rec.get("originCwd") or rec.get("cwd") or "")
        if branch and root and str(root) in repos and branch not in repos[str(root)]["branches"]:
            repos[str(root)]["branches"].append(branch)
    tar.add_json("repos.json", list(repos.values()))
    report.included["저장소 목록"] = len(repos)


def _export_memory(paths: Paths, tar: _Tar, sessions: list[dict], words, company_present: bool,
                   include_company: bool, report: Report) -> None:
    for s in sessions:
        for p in _transcript_paths(paths, [s["cliSessionId"]]):
            mem = p.parent / "memory"
            if mem.is_dir():
                for f in iter_files(mem):
                    if words and words.file_status(f) != "clean":
                        report.excluded["회사 내용 메모리 파일"] += 1
                        continue
                    tar.add_file(f"memory/projects/{p.parent.name}/memory/{f.relative_to(mem).as_posix()}", f)
    data = paths.home / ".agentmemory" / "data"
    if not data.is_dir():
        return
    if company_present and not include_company:
        report.notes.append("agentmemory: 회사 세션이 있어 기본 번들에서 뺌(--include-company 로 담을 수 있음)")
        return
    if agentmemory_running(paths.home):
        report.notes.append("agentmemory: 서버가 실행 중이라 건너뜀(서버를 멈춘 뒤 다시 실행)")
        return
    tar.add_tree("agentmemory/data", data)
    report.included["agentmemory 파일"] = sum(1 for _ in iter_files(data))


def cmd_export_bundle(args, paths: Paths) -> int:
    from session_move.cli import default_source
    source = args.source or default_source(paths)
    if not source or not paths.scope_dir(source).is_dir():
        print(f"원본 계정 폴더가 없습니다: {source}", file=sys.stderr)
        return 1
    items = parse_items(args.only, args.skip, set(ITEMS))
    out = Path(args.bundle)
    tmp = out.with_name(out.name + ".part")
    try:
        report = export_bundle(paths, source, tmp, items, args.include_company, args.no_company_check)
    except ExportRefused as e:
        tmp.unlink(missing_ok=True)
        print(str(e), file=sys.stderr)
        return 1
    os.replace(tmp, out)
    for line in report.lines():
        print(line)
    print(f"번들: {out} (권한 600. 옮긴 뒤 지우세요)")
    return 0


def _safe_extract(bundle: Path, dest: Path) -> None:
    with tarfile.open(bundle, "r:gz") as tar:
        for m in tar.getmembers():
            name = Path(m.name)
            if name.is_absolute() or ".." in name.parts:
                raise ValueError(f"번들에 허용되지 않는 경로가 있습니다: {m.name}")
            if not (m.isfile() or m.isdir() or m.issym()):
                continue
            target = dest / name
            if m.issym():
                target.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(m.linkname, target)
            elif m.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(m) as src, open(target, "wb") as f:
                    shutil.copyfileobj(src, f)
                os.chmod(target, 0o600)


def cmd_inspect_bundle(args, paths: Paths) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(args.bundle, "r:gz") as tar:
            manifest = json.loads(tar.extractfile("manifest.json").read())
            names = tar.getnames()
    counts = Counter(n.split("/", 1)[0] for n in names)
    print(f"원본: {manifest['sourceScope']} ({manifest['sourceHost']}, {manifest['createdAt']})")
    print(f"항목: {', '.join(manifest['items'])}")
    print("담긴 파일: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    print(f"세션 {len(manifest['sessions'])} · 예약 작업 {len(manifest['schedules'])} · backlog {len(manifest['backlog'])} · "
          f"그룹 {len(manifest['groups'])} · 프리셋 {len(manifest['presets'])}")
    if manifest["sourceHome"] != str(paths.home):
        print(f"경로가 다릅니다. 적용할 때 --map-path {manifest['sourceHome']}={paths.home} 를 주세요.")
    return 0


class _Plan:
    def __init__(self, home: Path):
        self.home = home
        self.roots: list[Path] = []
        self.ops: list = []

    def touch(self, dest: Path) -> None:
        p = dest
        while not p.parent.exists() and p.parent != p:
            p = p.parent
        if p not in self.roots:
            self.roots.append(p)

    def add(self, dest: Path, fn) -> None:
        self.touch(dest)
        self.ops.append(fn)


def _copy_into(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.is_symlink():
        os.symlink(os.readlink(src), dest)
    else:
        shutil.copy2(src, dest)


def cmd_apply_bundle(args, paths: Paths) -> int:
    pm = PathMap.parse(args.map_path)
    work = Path(tempfile.mkdtemp(prefix="session-move-"))
    try:
        _safe_extract(Path(args.bundle), work)
        return _apply_extracted(args, paths, work, pm)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _apply_extracted(args, paths: Paths, work: Path, pm: PathMap) -> int:
    manifest = load_json(work / "manifest.json")
    host = manifest.get("sourceHost") or "other"
    items = parse_items(args.only, args.skip, set(manifest["items"])) & set(manifest["items"])
    scopes = list_scopes(paths)
    target = args.to or (scopes[0].name if len(scopes) == 1 else None)
    if not target:
        print("대상 계정 폴더를 하나로 정할 수 없습니다. --to <계정ID>/<조직ID>로 지정하세요.", file=sys.stderr)
        return 1
    tdir = paths.scope_dir(target)
    if not tdir.is_dir():
        print(f"대상 계정 폴더가 없습니다: {target}", file=sys.stderr)
        return 1
    if manifest["sourceHome"] != str(paths.home) and not pm:
        print(f"원본과 HOME이 다릅니다. --map-path {manifest['sourceHome']}={paths.home} 를 주세요.", file=sys.stderr)
        return 1

    plan = _Plan(paths.home)
    report = Report()
    skipped = 0

    def side(dest: Path) -> Path:
        return dest if not (dest.exists() or dest.is_symlink()) else dest.with_name(f"{dest.name}.from-{host}")

    if "sessions" in items or "transcripts" in items:
        staged = work / "_sessions"
        staged.mkdir()
        for p in (work / "sessions").glob("*.json"):
            write_json(staged / p.name, remap_session(load_json(p), pm))
        m2 = dict(manifest, sessions=[{**s, "cwd": pm.apply(s.get("cwd", ""))} for s in manifest["sessions"]],
                  archived=load_json(work / "archived-ids.json", []) or [])
        troot = work / "transcripts" if "transcripts" in items else None
        actions = plan_sessions(m2, staged, tdir, troot, set(args.only_session) if args.only_session else None)
        actions = [a for a in actions if not (a.archived and a.reason == "원본 파일 없음")]
        skipped += sum(1 for a in actions if a.kind == "skip")
        plan.add(tdir, lambda a=actions, s=staged, t=tdir: apply_sessions(a, s, t))
        report.included["세션 반영"] = sum(1 for a in actions if a.kind != "skip")

    if "transcripts" in items and (work / "transcripts").is_dir():
        for folder in sorted(p for p in (work / "transcripts").iterdir() if p.is_dir()):
            new = paths.projects_root / pm.map_project_dir(folder.name)
            for f in iter_files(folder):
                dest = new / f.relative_to(folder)
                if dest.exists():
                    skipped += 1
                    continue
                fn = (lambda s=f, d=dest: rewrite_jsonl(s, d, pm)) if f.suffix == ".jsonl" else (lambda s=f, d=dest: _copy_into(s, d))
                plan.add(dest, fn)

    if "memory" in items:
        mem = work / "memory" / "projects"
        if mem.is_dir():
            for folder in mem.iterdir():
                for f in iter_files(folder):
                    dest = paths.projects_root / pm.map_project_dir(folder.name) / f.relative_to(folder)
                    if not dest.exists():
                        plan.add(dest, lambda s=f, d=dest: _copy_into(s, d))
        am = work / "agentmemory" / "data"
        if am.is_dir():
            dest = paths.home / ".agentmemory" / "data"
            if dest.exists() and any(dest.iterdir()):
                report.notes.append("agentmemory: 대상에 데이터가 있어 건너뜀")
                skipped += 1
            elif agentmemory_running(paths.home):
                report.notes.append("agentmemory: 서버가 실행 중이라 건너뜀")
                skipped += 1
            else:
                plan.add(dest, lambda d=dest: shutil.copytree(am, d, dirs_exist_ok=True))

    if "schedules" in items and (work / "scheduled-tasks.json").is_file():
        src = load_json(work / "scheduled-tasks.json")
        src["scheduledTasks"] = [remap_schedule(t, pm) for t in src.get("scheduledTasks", [])]
        dest = tdir / "scheduled-tasks.json"
        merged, added = merge_schedules(src, load_json(dest, {}), keep_enabled=False)
        if added:
            plan.add(dest, lambda d=dest, m=merged: write_json(d, m))
        for t in src["scheduledTasks"]:
            body = work / "scheduled-tasks" / t["id"] / "SKILL.md"
            bdest = Path(t.get("filePath") or paths.claude_dir / "scheduled-tasks" / t["id"] / "SKILL.md")
            if t["id"] in added and body.is_file() and not bdest.exists():
                plan.add(bdest, lambda s=body, d=bdest: _copy_into(s, d))
        report.notes.append(f"예약 작업 {len(added)}개를 꺼진 상태로 추가(finish 단계에서 켤 것을 고르세요)")

    if "backlog" in items and (work / "backlog" / "tasks.json").is_file():
        src = load_json(work / "backlog" / "tasks.json")
        src["items"] = [remap_backlog_item(b, pm) for b in src.get("items", [])]
        dest = tdir / "backlog" / "tasks.json"
        merged, changed = merge_backlog(src, load_json(dest, {}))
        if changed:
            plan.add(dest, lambda d=dest, m=merged: write_json(d, m))

    claude_src = work / "claude"
    if "instructions" in items and claude_src.is_dir():
        if (claude_src / "CLAUDE.md").is_file():
            d = side(paths.claude_dir / "CLAUDE.md")
            plan.add(d, lambda d=d: _copy_into(claude_src / "CLAUDE.md", d))
        if (claude_src / "settings.json").is_file():
            d = side(paths.claude_dir / "settings.json")
            data = remap_settings(load_json(claude_src / "settings.json"), pm)
            plan.add(d, lambda d=d, data=data: write_json(d, data))
        cmds = claude_src / "commands"
        if cmds.is_dir():
            for f in iter_files(cmds):
                d = side(paths.claude_dir / "commands" / f.relative_to(cmds))
                plan.add(d, lambda s=f, d=d: _copy_into(s, d))

    if "skills" in items:
        for prefix, base in (("agents-skills", paths.home / ".agents" / "skills"),
                             ("claude/skills", paths.claude_dir / "skills")):
            root = work / prefix
            if not root.is_dir():
                continue
            for unit in sorted(root.iterdir()):
                dest = base / unit.name
                if dest.exists() or dest.is_symlink():
                    skipped += 1
                    continue
                if unit.is_symlink():
                    link = pm.apply(os.readlink(unit))
                    plan.add(dest, lambda d=dest, l=link: (d.parent.mkdir(parents=True, exist_ok=True), os.symlink(l, d)))
                else:
                    plan.add(dest, lambda s=unit, d=dest: shutil.copytree(s, d, symlinks=True))

    if "mcp" in items and (work / "mcp.json").is_file():
        d = side(paths.claude_dir / "mcp.json")
        plan.add(d, lambda d=d: _copy_into(work / "mcp.json", d))
        report.notes.append(f"MCP 정의를 {d.name} 에 두었습니다. 비운 env·헤더 값을 채우세요.")

    if "repos" in items and (work / "repos.json").is_file():
        for r in load_json(work / "repos.json", []):
            dest = Path(pm.apply(r["path"]))
            if dest.exists():
                report.notes.append(f"저장소 {dest}: 이미 있음")
                continue
            if not args.clone:
                report.notes.append(f"저장소 {dest}: {r['url']} (--clone 으로 받을 수 있음)")
                continue
            plan.add(dest, lambda r=r, d=dest: _clone(r, d, report))

    if "plugins" in items and (work / "plugins.json").is_file():
        pl = load_json(work / "plugins.json")
        for name, src in (pl.get("marketplaces") or {}).items():
            repo = (src or {}).get("repo") or (src or {}).get("url")
            report.notes.append(f"마켓플레이스 {name}: /plugin marketplace add {repo}")
        for p in pl.get("installed", []):
            report.notes.append(f"플러그인: /plugin install {p}")

    print(f"대상: {target}")
    if args.dry_run:
        print(f"쓸 곳 {len(plan.roots)}곳, 작업 {len(plan.ops)}개(미리보기)")
        for line in report.lines():
            print(line)
        return 0
    if app_is_running():
        print("Claude 앱이 실행 중입니다. 앱을 완전히 종료(⌘Q)한 뒤 다시 실행하세요.", file=sys.stderr)
        return 3
    print(f"백업: {backup(plan.roots, paths.state_dir)}")
    for fn in plan.ops:
        fn()
    for line in report.lines():
        print(line)
    print("같은 세션을 두 기기에서 동시에 이어가지 마세요.")
    return 2 if skipped else 0


def _clone(r: dict, dest: Path, report: Report) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    res = subprocess.run(["git", "clone", "-q", r["url"], str(dest)], capture_output=True, text=True)
    if res.returncode != 0:
        report.notes.append(f"저장소 {dest}: 클론 실패")
        print(f"저장소 {dest}: 클론 실패", file=sys.stderr)
        return
    for branch in r.get("branches", []):
        heads = subprocess.run(["git", "-C", str(dest), "ls-remote", "--heads", "origin", branch],
                               capture_output=True, text=True).stdout.strip()
        if not heads:
            print(f"저장소 {dest}: 브랜치 {branch} 원격에 없음 — 건너뜀")
            continue
        subprocess.run(["git", "-C", str(dest), "branch", "--track", branch, f"origin/{branch}"], capture_output=True)
