import json
import stat
import sys
from pathlib import Path

_COMMON = r'''
import json, os, sys, time
from pathlib import Path

log = Path(os.environ["FAKE_CLI_LOG"])
log.mkdir(parents=True, exist_ok=True)


def claim(name):
    n = 1
    while True:
        try:
            fd = os.open(log / f"{name}-{n}.args.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError:
            n += 1
            continue
        with os.fdopen(fd, "w") as f:
            json.dump(sys.argv[1:], f)
        return n


def stamp(name, n, event):
    with open(log / f"{name}-{n}.{event}", "w") as f:
        f.write(repr(time.time()))


def commit(root):
    import subprocess
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.email=fake@example.com", "-c", "user.name=fake",
                    "commit", "-q", "--no-verify", "--allow-empty", "-m", "fake cli commit"], check=True, capture_output=True)


def mutate(root):
    spec = os.environ.get("FAKE_MUTATE", "")
    for item in filter(None, spec.split(";")):
        rel, _, content = item.partition("=")
        target = Path(root) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    for key, files in json.loads(os.environ.get("FAKE_MUTATE_WT", "{}")).items():
        if key in Path(root).name:
            for rel, content in files.items():
                target = Path(root) / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
    if os.environ.get("FAKE_COMMIT"):
        commit(root)
'''

CODEX_BODY = _COMMON + r'''
if sys.argv[1:2] == ["sandbox"]:
    import subprocess
    n = claim("codex-sandbox")
    args = sys.argv[2:]
    (log / f"codex-sandbox-{n}.env.json").write_text(json.dumps({"CODEX_HOME": os.environ.get("CODEX_HOME")}))
    command = args[args.index("--") + 1:]
    if command[-3:] == ["sh", "-c", ":"]:
        probe_exit = os.environ.get("FAKE_CODEX_SANDBOX_PROBE_EXIT")
        if probe_exit:
            sys.stderr.write(os.environ.get("FAKE_CODEX_SANDBOX_PROBE_STDERR", ""))
            sys.exit(int(probe_exit))
    elif os.environ.get("FAKE_CODEX_SANDBOX_EXIT"):
        sys.exit(int(os.environ["FAKE_CODEX_SANDBOX_EXIT"]))
    sys.exit(subprocess.run(command, cwd=args[args.index("-C") + 1]).returncode)
n = claim("codex")
stamp("codex", n, "start")
args = sys.argv[1:]
home = Path(os.environ["CODEX_HOME"])
(log / f"codex-{n}.env.json").write_text(json.dumps({
    k: os.environ.get(k) for k in ("CODEX_HOME", "UV_CACHE_DIR", "PIP_CACHE_DIR", "XDG_CACHE_HOME", "npm_config_cache")}))
(log / f"codex-{n}.config.toml").write_text((home / "config.toml").read_text())
(log / f"codex-{n}.auth-is-link").write_text(str((home / "auth.json").is_symlink()))
if args and args[-1] == "-":
    (log / f"codex-{n}.stdin").write_text(sys.stdin.read())
sleep = float(os.environ.get("FAKE_CODEX_SLEEP", "0"))
if sleep:
    time.sleep(sleep)
mutate(args[args.index("-C") + 1])
if os.environ.get("FAKE_CODEX_REFRESH"):
    (home / "auth.json").write_text(os.environ["FAKE_CODEX_REFRESH"])
if os.environ.get("FAKE_CODEX_TOUCH_ORIGINAL"):
    Path(os.environ["FAKE_CODEX_ORIGINAL_AUTH"]).write_text(os.environ["FAKE_CODEX_TOUCH_ORIGINAL"])
if "-o" in args and "FAKE_CODEX_RESULT" in os.environ:
    Path(args[args.index("-o") + 1]).write_text(os.environ["FAKE_CODEX_RESULT"])
print(json.dumps({"type": "thread.started"}))
for event in json.loads(os.environ.get("FAKE_CODEX_EVENTS", "[]")):
    print(json.dumps(event))
print(json.dumps({"type": "turn.completed", "usage": {
    "input_tokens": 11, "cached_input_tokens": 3, "output_tokens": 7, "reasoning_output_tokens": 2}}))
sys.stderr.write(os.environ.get("FAKE_CODEX_STDERR", ""))
stamp("codex", n, "end")
sys.exit(int(os.environ.get("FAKE_CODEX_EXIT", "0")))
'''

CURSOR_BODY = _COMMON + r'''
def cursor_entry(n, workspace):
    responses = json.loads(os.environ.get("FAKE_CURSOR_RESULTS", '[""]'))
    matched = [e for e in responses if isinstance(e, dict) and e.get("match") and e["match"] in workspace]
    plain = [e for e in responses if not (isinstance(e, dict) and "match" in e)] or [""]
    entry = matched[0] if matched else plain[min(n, len(plain)) - 1]
    return {"result": entry} if isinstance(entry, str) else entry


def acp_send(message):
    print(json.dumps({"jsonrpc": "2.0", **message}, ensure_ascii=False), flush=True)


def acp_options(values):
    models = json.loads(os.environ.get("FAKE_ACP_MODELS", '["auto-smart", "composer-2.5"]'))
    return [{"id": key, "currentValue": value, "options": [{"value": m} for m in models] if key == "model" else []}
            for key, value in values.items()]


def acp_chunk(session, text):
    acp_send({"method": "session/update", "params": {"sessionId": session, "update": {
        "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}}})


def acp_client_request(method, params):
    acp_send({"id": "server-request", "method": method, "params": params})
    return json.loads(sys.stdin.readline())


def acp_linger():
    import subprocess
    code = ("import os, pathlib, signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "home = pathlib.Path(os.environ['HOME'])\n"
            "(home / 'linger.pid').write_text(str(os.getpid()))\n"
            "end = time.monotonic() + 30\n"
            "while time.monotonic() < end:\n"
            "    (home / 'Library' / 'linger').mkdir(parents=True, exist_ok=True)\n"
            "    time.sleep(0.01)\n")
    subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)
    while not (Path(os.environ["HOME"]) / "linger.pid").exists():
        time.sleep(0.01)


def acp_prompt(server, request, session, state):
    text = request["params"]["prompt"][0]["text"]
    m = claim("acp-prompt")
    record = {"server": server, "pid": os.getpid(), "sessionId": session, "cwd": state["cwd"], "prompt": text,
              "mcpServers": state["mcpServers"], "values": dict(state["values"])}
    mutate(state["cwd"])
    sleep = float(os.environ.get("FAKE_CURSOR_SLEEP", "0"))
    if sleep:
        time.sleep(sleep)
    entry = cursor_entry(m, state["cwd"])
    sys.stderr.write(entry.get("stderr", ""))
    sys.stderr.flush()
    if "stdout" in entry:
        sys.stderr.write(entry["stdout"])
        sys.stderr.flush()
        (log / f"acp-prompt-{m}.json").write_text(json.dumps(record, ensure_ascii=False))
        sys.exit(entry.get("exit", 1))
    if os.environ.get("FAKE_ACP_TOOL"):
        acp_chunk(session, "파일을 읽겠습니다")
        acp_send({"method": "session/update", "params": {"sessionId": session, "update": {
            "sessionUpdate": "tool_call", "toolCallId": "tool-1", "status": "pending"}}})
        options = json.loads(os.environ.get("FAKE_ACP_PERMISSION_OPTIONS", "null")) or [
            {"optionId": "allow-once", "kind": "allow_once"}, {"optionId": "allow-always", "kind": "allow_always"},
            {"optionId": "reject-once", "kind": "reject_once"}]
        record["permission_response"] = acp_client_request(
            "session/request_permission", {"sessionId": session, "toolCall": {"kind": "execute"}, "options": options})
    if os.environ.get("FAKE_ACP_CLIENT_REQ"):
        record["client_response"] = acp_client_request("fs/read_text_file", {"sessionId": session, "path": "/secret"})
    (log / f"acp-prompt-{m}.json").write_text(json.dumps(record, ensure_ascii=False))
    if entry.get("is_error") or entry.get("exit"):
        message = entry.get("result", "") if entry.get("is_error") else f"exit {entry['exit']}"
        if entry.get("echo_prompt"):
            message = f"{message}\n{text}"
        acp_send({"id": request["id"], "error": {"code": -32603, "message": message}})
        return
    answer = entry.get("result", "")
    for offset in range(0, len(answer), 11):
        acp_chunk(session, answer[offset:offset + 11])
    acp_send({"id": request["id"], "result": {"stopReason": entry.get("stop", "end_turn")}})


def run_acp():
    server = claim("cursor-acp")
    home = Path(os.environ["HOME"])
    (log / f"cursor-acp-{server}.env.json").write_text(json.dumps({
        k: os.environ.get(k) for k in ("HOME", "PYENV_ROOT", "UV_CACHE_DIR", "XDG_CACHE_HOME", "npm_config_cache")}))
    (log / f"cursor-acp-{server}.pid").write_text(str(os.getpid()))
    config = home / ".cursor" / "cli-config.json"
    if config.is_file():
        (log / f"cursor-acp-{server}.config.json").write_text(config.read_text())
    if os.environ.get("FAKE_ACP_LINGER"):
        acp_linger()
    sessions = {}
    for line in sys.stdin:
        request = json.loads(line)
        method, params = request.get("method"), request.get("params", {})
        if method == "initialize":
            (log / f"cursor-acp-{server}.initialize.json").write_text(json.dumps(params))
            if os.environ.get("FAKE_ACP_INIT_ERROR"):
                acp_send({"id": request["id"], "error": {"code": -32603, "message": os.environ["FAKE_ACP_INIT_ERROR"]}})
                continue
            result = {"protocolVersion": 1}
        elif method == "session/new":
            session = f"{os.getpid()}-s{len(sessions) + 1}"
            sessions[session] = {"cwd": params.get("cwd"), "mcpServers": params.get("mcpServers"),
                                 "values": {"mode": "agent", "model": "auto-smart"}}
            result = {"sessionId": session, "configOptions": acp_options(sessions[session]["values"])}
        elif method == "session/set_config_option":
            values = sessions[params["sessionId"]]["values"]
            key, value = params["configId"], params["value"]
            models = json.loads(os.environ.get("FAKE_ACP_MODELS", '["auto-smart", "composer-2.5"]'))
            if key == "model" and value not in models:
                acp_send({"id": request["id"], "error": {"code": -32602, "message": f"Invalid model value: {value}"}})
                continue
            if key != os.environ.get("FAKE_ACP_IGNORE_CONFIG"):
                values[key] = value
                if key == "model":
                    values["fast"] = "true"
            result = {"configOptions": acp_options(values)}
        elif method == "session/prompt":
            acp_prompt(server, request, params["sessionId"], sessions[params["sessionId"]])
            continue
        elif "id" in request and "method" not in request:
            continue
        else:
            acp_send({"id": request["id"], "error": {"code": -32601, "message": "지원하지 않는다"}})
            continue
        acp_send({"id": request["id"], "result": result})
    sys.exit(0)


if sys.argv[1:] == ["status"]:
    claim("cursor-status")
    print(os.environ.get("FAKE_CURSOR_STATUS", "✓ Logged in as tester@example.com"))
    sys.exit(0)
if sys.argv[1:] == ["acp"]:
    run_acp()
n = claim("cursor-agent")
stamp("cursor-agent", n, "start")
home = Path(os.environ["HOME"])
(log / f"cursor-agent-{n}.stdin").write_text(sys.stdin.read())
(log / f"cursor-agent-{n}.env.json").write_text(json.dumps({
    k: os.environ.get(k) for k in (
        "HOME", "PYENV_ROOT", "UV_PYTHON_INSTALL_DIR", "UV_CACHE_DIR", "PIP_CACHE_DIR", "XDG_CACHE_HOME", "npm_config_cache")}))
config = home / ".cursor" / "cli-config.json"
if config.is_file():
    (log / f"cursor-agent-{n}.config.json").write_text(config.read_text())
sleep = float(os.environ.get("FAKE_CURSOR_SLEEP", "0"))
if sleep:
    time.sleep(sleep)
workspace = sys.argv[sys.argv.index("--workspace") + 1]
mutate(workspace)
entry = cursor_entry(n, workspace)
if "stdout" in entry:
    sys.stdout.write(entry["stdout"])
else:
    print(json.dumps({
        "type": "result", "subtype": "success", "is_error": entry.get("is_error", False),
        "result": entry.get("result", ""), "session_id": "s",
        "usage": {"inputTokens": 100, "outputTokens": 20, "cacheReadTokens": 5, "cacheWriteTokens": 1}}))
sys.stderr.write(entry.get("stderr", ""))
stamp("cursor-agent", n, "end")
sys.exit(entry.get("exit", 0))
'''

CLAUDE_BODY = _COMMON + r'''
n = claim("claude")
stamp("claude", n, "start")
args = sys.argv[1:]
(log / f"claude-{n}.stdin").write_text(sys.stdin.read())
(log / f"claude-{n}.env.json").write_text(json.dumps({"CLAUDECODE": os.environ.get("CLAUDECODE")}))
(log / f"claude-{n}.cwd").write_text(os.getcwd())
sleep = float(os.environ.get("FAKE_CLAUDE_SLEEP", "0"))
if sleep:
    time.sleep(sleep)
mutate(os.getcwd())
model = args[args.index("--model") + 1]
envelope = {
    "type": "result", "subtype": "success", "is_error": False, "result": "", "session_id": "s",
    "total_cost_usd": 0.25, "num_turns": 2,
    "usage": {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 5, "cache_creation_input_tokens": 1},
    "modelUsage": {model: {"inputTokens": 100}},
}
if "FAKE_CLAUDE_RESULT" in os.environ:
    envelope["structured_output"] = json.loads(os.environ["FAKE_CLAUDE_RESULT"])
model_result = json.loads(os.environ.get("FAKE_CLAUDE_RESULTS_MODELS", "{}")).get(model)
if model_result is not None:
    envelope["structured_output"] = model_result
envelope.update(json.loads(os.environ.get("FAKE_CLAUDE_ENVELOPE", "{}")))
if os.environ.get("FAKE_CLAUDE_STDOUT"):
    sys.stdout.write(os.environ["FAKE_CLAUDE_STDOUT"])
else:
    print(json.dumps(envelope, ensure_ascii=False))
sys.stderr.write(os.environ.get("FAKE_CLAUDE_STDERR", ""))
stamp("claude", n, "end")
sys.exit(int(os.environ.get("FAKE_CLAUDE_EXIT", "0")))
'''


def install(directory, name, body):
    path = Path(directory) / name
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def result_json(**overrides):
    result = {"verdict": "사실", "needs_design_decision": False, "reason": "재현함", "verify": [], "model_id": "self-reported"}
    result.update(overrides)
    return json.dumps(result, ensure_ascii=False)


def fenced(text):
    return f"설명\n```json\n{text}\n```\n"
