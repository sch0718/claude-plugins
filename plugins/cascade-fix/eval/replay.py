import argparse
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_ROOT))
from cascade import assignment, cli_steps  # noqa: E402
from cascade.jobs import RUNNER_CLAUDE  # noqa: E402


def default_repos_toml() -> Path:
    sys.path.insert(0, str(SKILL_ROOT))
    from cascade.cli_steps import repos_toml

    return repos_toml()
REQUIRED = ("id", "repo", "commit", "source", "finding", "truth", "baseline_opus_calls")
SOURCES = ("codex-doc", "codex-code", "claude-doc", "claude-code", "cursor-doc", "cursor-code", "pr-comment")
ROUND = "1"
WORKERS_CLAUDE, WORKERS_EXTERNAL = "claude", "external"
FINDINGS_INPUT = "findings-input.json"
INIT_LINE_PREFIXES = ("프로필:", "덮음:", "대체:", "역할:", "전송 동의:")


def load_sample(path) -> list[dict]:
    items = []
    for no, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        item = json.loads(line)
        missing = [k for k in REQUIRED if k not in item]
        if missing:
            raise ValueError(f"{path}:{no} 에 {', '.join(missing)} 가 없다")
        if item["source"] not in SOURCES:
            raise ValueError(f"{path}:{no} 알 수 없는 source: {item['source']}")
        items.append(item)
    return items


def git(cwd, *args, check=True) -> str:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 실패: {(p.stderr or p.stdout).strip()}")
    return p.stdout.strip()


def toml_value(value) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{json.dumps(k, ensure_ascii=False)} = {toml_value(v)}" for k, v in value.items()) + "}"
    return json.dumps(value, ensure_ascii=False)


def write_worktree_toml(src: Path, repo: Path, wt: Path, dest: Path) -> None:
    data = tomllib.loads(src.read_text(encoding="utf-8")) if src.is_file() else {}
    root = repo.resolve()
    entry = next((v for k, v in data.get("repos", {}).items() if Path(k).expanduser().resolve() == root), None)
    lines = [f"default_profile = {toml_value(data['default_profile'])}"] if "default_profile" in data else []
    lines.append("[repos]")
    if entry is not None:
        key = json.dumps(str(wt))
        tables = {k: v for k, v in entry.items() if isinstance(v, dict)}
        lines += [f"[repos.{key}]", *(f"{k} = {toml_value(v)}" for k, v in entry.items() if k not in tables)]
        for name, table in tables.items():
            lines += [f"[repos.{key}.{name}]", *(f"{k} = {toml_value(v)}" for k, v in table.items())]
    for name, table in data.get("profiles", {}).items():
        lines += [f"[profiles.{json.dumps(name, ensure_ascii=False)}]",
                  *(f"{json.dumps(k, ensure_ascii=False)} = {toml_value(v)}" for k, v in table.items())]
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")


def cli(wt: Path, env: dict, *args) -> str:
    p = subprocess.run([sys.executable, "-m", "cascade.cli", *args], cwd=wt, env=env, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"cli {args[0]} 실패({p.returncode}): {(p.stdout + p.stderr).strip()[-1500:]}")
    return p.stdout


def doc_arg(doc: str, repo: Path, wt: Path) -> str:
    path = Path(doc)
    if not path.is_absolute():
        return str(wt / path)
    try:
        return str(wt / path.relative_to(repo))
    except ValueError:
        return doc


def write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def finish_item(base: Path, repo: Path, wt: Path, run_dir: Path | None, env: dict) -> None:
    errors = []
    try:
        if run_dir is not None and (run_dir / "ledger.json").is_file():
            try:
                cli(wt, env, "cleanup", "--run-dir", str(run_dir))
            except RuntimeError as e:
                errors.append(str(e))
            shutil.rmtree(base / "run", ignore_errors=True)
            shutil.copytree(run_dir, base / "run")
            shutil.copyfile(run_dir / "ledger.json", base / "ledger.json")
    finally:
        if wt.exists():
            git(repo, "worktree", "remove", "--force", str(wt), check=False)
    if errors:
        raise RuntimeError("; ".join(errors))


def base_env(toml: Path) -> dict:
    env = dict(os.environ)
    env.update(CASCADE_REPOS_TOML=str(toml), PYTHONPATH=str(SKILL_ROOT), PYTHONDONTWRITEBYTECODE="1")
    return env


def has_jobs(path: Path) -> bool:
    return path.is_file() and bool(read_json(path).get("jobs"))


def init_args(item: dict, wt: Path, *, profile: str | None, workers: str | None, adopt_on_both_pass: bool) -> list[str]:
    repo = Path(item["repo"]).resolve()
    args = ["init", "--source", item["source"], "--findings-json", str(wt.parent / FINDINGS_INPUT), "--dry-run"]
    if profile:
        args += ["--profile", profile]
    if workers:
        args += ["--workers", workers]
    if item.get("doc"):
        args += ["--doc", doc_arg(item["doc"], repo, wt)]
    if adopt_on_both_pass:
        args.append("--adopt-on-both-pass")
    return args


def worker_cells(ledger: dict) -> dict[str, dict]:
    roles, mode = ledger["profile"]["roles"], ledger["mode"]
    return {assignment.job_cell_key(mode, role): cell for role, cell in assignment.mode_workers(roles, mode).items()}


def is_external(cell: dict) -> bool:
    return RUNNER_CLAUDE != cell["runner"] or RUNNER_CLAUDE != (cell.get("fallback") or cell)["runner"]


def external_cells(ledger: dict) -> list[str]:
    return [f"{key}={cell['runner']}(대체 {(cell.get('fallback') or cell)['runner']})"
            for key, cell in worker_cells(ledger).items() if is_external(cell)]


def confirmed_roles(ledger: dict) -> dict[str, dict]:
    roles = ledger["profile"]["roles"]
    adjudicator = f"{ledger['mode']}.adjudicator"
    return {**worker_cells(ledger), adjudicator: roles[adjudicator]}


def roles_text(roles: dict[str, dict]) -> str:
    return ", ".join(f"{key}={cell['runner']}({cell['model'] or '기본'}, {cell['effort']})" for key, cell in roles.items())


def echo_init(item_id: str, base: Path, text: str) -> None:
    lines = [line for line in text.splitlines() if line.startswith(INIT_LINE_PREFIXES)]
    (base / "init.txt").write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    for line in lines:
        print(f"{item_id}: {line}")


def refuse_external(ledger: dict) -> None:
    external = external_cells(ledger)
    if external:
        raise RuntimeError(f"--fake-results는 모델을 부르지 않으므로 외부 runner 칸이 있는 구성은 돌릴 수 없다: {', '.join(external)}")


def run_item(item: dict, out: Path, repos_toml: Path, fake: Path | None, workers: str | None = None,
             adopt_on_both_pass: bool = False, profile: str | None = None) -> None:
    repo = Path(item["repo"]).resolve()
    base = (out / item["id"]).resolve()
    wt = base / "wt"
    base.mkdir(parents=True, exist_ok=True)
    meta = {"truth": item["truth"], "baseline_opus_calls": item["baseline_opus_calls"]}
    write_json(base / "meta.json", meta)
    run_dir = None
    env = base_env(base / "repos.toml")
    try:
        if not wt.exists():
            no_hooks = base / "no-hooks"
            no_hooks.mkdir(exist_ok=True)
            git(repo, "-c", f"core.hooksPath={no_hooks}", "worktree", "add", "--detach", str(wt), item["commit"])
        else:
            head, wanted = git(wt, "rev-parse", "HEAD"), git(repo, "rev-parse", "--verify", f"{item['commit']}^{{commit}}")
            if head != wanted:
                raise RuntimeError(f"기존 워크트리의 HEAD {head[:12]}가 표본의 커밋 {wanted[:12]}와 다르다")
        wt = wt.resolve()
        write_worktree_toml(repos_toml, repo, wt, base / "repos.toml")
        write_json(base / FINDINGS_INPUT, [item["finding"]])
        out_text = cli(wt, env, *init_args(item, wt, profile=profile, workers=workers, adopt_on_both_pass=adopt_on_both_pass))
        run_dir = Path(next(x for x in out_text.splitlines() if x.startswith("run_dir=")).split("=", 1)[1])
        rd = str(run_dir)
        echo_init(item["id"], base, out_text)
        if fake is not None:
            refuse_external(read_json(run_dir / "ledger.json"))
        cli(wt, env, "jobs", "--run-dir", rd, "--round", ROUND)
        meta["roles"] = confirmed_roles(read_json(run_dir / "ledger.json"))
        write_json(base / "meta.json", meta)
        print(f"{item['id']}: 역할 확정: {roles_text(meta['roles'])}")
        round_dir = run_dir / "round-1"
        if (round_dir / "stage1-cli-args.json").is_file():
            cli(wt, env, "workers", "--run-dir", rd, "--round", ROUND, "--stage", "stage1")
        if fake is None:
            write_json(base / "meta.json", {**meta, "pending_run_dir": rd})
            if (round_dir / "stage1-cli.json").is_file():
                print(f"{item['id']}: CLI 작업은 workers 로 끝났다. 남은 Claude 작업 파일: {round_dir / 'stage1-args.json'}"
                      f", {round_dir / 'stage1-fallback-args.json'}")
            else:
                print(f"{item['id']}: 작업 파일 {round_dir / 'stage1-args.json'}")
            print("  스킬의 Workflow로 남은 stage1 작업을 돌리고 judge → adjudicate → apply → report 를 이어간 뒤 --collect 로 모은다")
            return
        item_fake = fake / item["id"]
        stage1 = round_dir / "stage1.json"
        judge = ["judge", "--run-dir", rd, "--round", ROUND]
        if has_jobs(round_dir / "stage1-args.json"):
            shutil.copyfile(item_fake / "stage1.json", stage1)
            judge += ["--results", str(stage1)]
        if (item_fake / "checker.json").is_file():
            checker = round_dir / "checker.json"
            shutil.copyfile(item_fake / "checker.json", checker)
            judge += ["--checker-results", str(checker)]
        cli(wt, env, *judge)
        if (item_fake / "adj.json").is_file() and (round_dir / "adj-args.json").is_file():
            adj = round_dir / "adj.json"
            shutil.copyfile(item_fake / "adj.json", adj)
            cli(wt, env, "adjudicate", "--run-dir", rd, "--round", ROUND, "--results", str(adj))
        cli(wt, env, "apply", "--run-dir", rd, "--round", ROUND)
        cli(wt, env, "report", "--run-dir", rd)
    except Exception as e:
        write_json(base / "meta.json", {**meta, "error": str(e)})
        finish_item(base, repo, wt, run_dir, env)
        raise
    finish_item(base, repo, wt, run_dir, env)


def unfinished(run_dir: Path) -> list[str]:
    ledger = cli_steps.load_ledger(run_dir)
    first, last = ledger.rounds[0], ledger.rounds[-1]
    missing = [] if cli_steps.step_done(first, "apply") else ["1회차 apply 완료 이벤트"]
    if not (run_dir / f"round-{last.no}" / "apply.json").is_file():
        missing.append(f"회차 {last.no} apply.json")
    for rnd in ledger.rounds:
        missing += [f"회차 {rnd.no} 의견 {fid}" for fid in cli_steps.pending_opinions(ledger, run_dir / f"round-{rnd.no}")]
    if not (run_dir / "report.md").is_file():
        missing.append("report.md")
    if not any(e.get("step") == "report" and e.get("round") == last.no for e in last.events):
        missing.append("원장 report 이벤트")
    return missing


def collect_item(item: dict, out: Path, repos_toml: Path) -> bool:
    base = (out / item["id"]).resolve()
    meta_path = base / "meta.json"
    if not meta_path.is_file():
        return False
    meta = read_json(meta_path)
    pending = meta.get("pending_run_dir")
    if not pending:
        return False
    missing = unfinished(Path(pending))
    if missing:
        write_json(meta_path, {**meta, "incomplete": True})
        print(f"{item['id']}: 끝나지 않았다 — {', '.join(missing)}")
        return False
    repo = Path(item["repo"]).resolve()
    wt = (base / "wt").resolve()
    finish_item(base, repo, wt, Path(pending), base_env(base / "repos.toml"))
    meta.pop("pending_run_dir")
    meta.pop("incomplete", None)
    write_json(meta_path, meta)
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="소급 재생: 표본 지적을 그 시점 커밋에서 dry-run으로 다시 돌린다")
    parser.add_argument("--sample", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--profile", help="init에 넘긴다. 역할 프로필 이름(주지 않으면 init의 기본 선택)")
    parser.add_argument("--workers", choices=(WORKERS_CLAUDE, WORKERS_EXTERNAL),
                        help="init에 넘긴다(주지 않으면 넘기지 않는다). external이면 CLI 작업을 workers 단계로 돌리고 "
                        "남은 Claude 작업만 Workflow에 맡긴다. --fake-results와 함께 쓸 수 없다")
    parser.add_argument("--fake-results", help="<id>/stage1.json(·adj.json·checker.json)을 두는 폴더. 모델을 부르지 않는다. "
                        "모델을 부르는 external 작업자와 함께 쓸 수 없고, init 뒤 확정 배정에 외부 runner(또는 외부 대체)가 "
                        "있는 항목은 jobs 전에 오류로 멈춘다")
    parser.add_argument("--repos-toml", default=str(default_repos_toml()))
    parser.add_argument("--adopt-on-both-pass", action="store_true",
                        help="init에 넘긴다(켜기만 한다). 같은 판정에 패치가 달라도 둘 다 검증을 통과하면 판가름 없이 작은 패치를 채택")
    parser.add_argument("--collect", action="store_true",
                        help="모델을 부르는 실제 실행이 끝난 항목의 원장을 모으고 정리한다. 1회차 apply 완료 이벤트·마지막 회차 apply.json·"
                        "남은 의견 단계 없음·report.md와 원장의 report 이벤트가 모두 있어야 끝난 것으로 본다")
    args = parser.parse_args(argv)
    if args.fake_results and args.workers == WORKERS_EXTERNAL:
        parser.error("--fake-results는 모델을 부르지 않으므로 --workers external과 함께 쓸 수 없다(external은 Codex·Cursor를 부른다)")
    items = load_sample(args.sample)[: args.limit]
    out, toml = Path(args.out), Path(args.repos_toml)
    out.mkdir(parents=True, exist_ok=True)
    fake = Path(args.fake_results).resolve() if args.fake_results else None
    failed = 0
    for item in items:
        try:
            if args.collect:
                print(f"{item['id']}: {'수집' if collect_item(item, out, toml) else '건너뜀(대기 중이 아니거나 끝나지 않음)'}")
            else:
                run_item(item, out, toml, fake, args.workers, args.adopt_on_both_pass, args.profile)
                print(f"{item['id']}: 완료" if fake else "")
        except Exception as e:
            failed += 1
            print(f"{item['id']}: 오류 — {str(e).splitlines()[0] if str(e) else type(e).__name__}")
    print(f"항목 {len(items)}건, 오류 {failed}건")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
