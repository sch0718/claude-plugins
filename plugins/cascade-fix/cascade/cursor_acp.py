"""cursor-agent acp(Agent Client Protocol) 상주 서버 풀. 읽기 역할 작업만 보낸다.

ACP에서는 셸이 샌드박스 없이 돌기 때문에(2026-10-21 실측) 셸·쓰기를 막은 프로필로만 띄운다.
ACP는 읽기 경계(sandbox.readBoundary)를 쓰지 않아(2026-10-23 실측) 작업 폴더 밖 Read 거부 규칙을 서버마다 만들므로,
서버 하나는 작업 폴더 하나만 맡는다.
"""
import atexit
import codecs
import json
import os
import queue
import select
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

SESSIONS_PER_SERVER = 50
START_TIMEOUT = 180
STOP_GRACE = 5
STDERR_LIMIT = 1024 * 1024
SETTINGS = {"fast": "false", "mode": "ask"}

_LIVE: set["_Server"] = set()
_LIVE_LOCK = threading.Lock()


class AcpError(Exception):
    """ACP 호출 실패. 메시지에 보낸 프롬프트 원문은 없다."""


class AcpTimeout(AcpError):
    pass


def _close_live() -> None:
    with _LIVE_LOCK:
        servers = list(_LIVE)
    for server in servers:
        server.close()


atexit.register(_close_live)


def _scrub(text: str, secrets: list[str]) -> str:
    for secret in sorted({s.strip() for s in secrets if s.strip()}, key=len, reverse=True):
        text = text.replace(secret, "")
    return text


class _Server:
    """임시 폴더(HOME·캐시)를 가진 cursor-agent acp 프로세스 하나. env_factory(임시 폴더, 작업 폴더)로 환경을 만든다."""

    def __init__(self, exe: str, env_factory: Callable[[Path, Path], dict], worktree: Path):
        self.exe = exe
        self.env_factory = env_factory
        self.worktree = worktree
        self.root: Path | None = None
        self.proc: subprocess.Popen | None = None
        self.sessions = 0
        self._messages: queue.Queue = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._stderr = ""
        self._stderr_at = 0.0
        self._stderr_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._closed = False
        self._next_id = 0
        with _LIVE_LOCK:
            _LIVE.add(self)

    def start(self, timeout: float) -> None:
        with self._close_lock:
            if self._closed:
                raise AcpError("Cursor ACP 서버를 닫았다")
            if self.proc is not None:
                return
            self.root = Path(tempfile.mkdtemp(prefix="cascade-cursor-acp-"))
            try:
                env = self.env_factory(self.root, self.worktree)
            except OSError as e:
                raise AcpError(f"Cursor 프로필을 만들지 못했다: {e}") from e
            try:
                self.proc = subprocess.Popen(
                    [self.exe, "acp"], cwd=self.root, env=env,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8", errors="replace", start_new_session=True)
            except OSError as e:
                raise AcpError(f"실행 파일을 실행하지 못했다: {e}") from e
            os.set_blocking(self.proc.stdin.fileno(), False)
            for target in (self._read_stdout, self._read_stderr):
                thread = threading.Thread(target=target, daemon=True)
                self._threads.append(thread)
                thread.start()
        self.request("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {
                "fs": {"readTextFile": False, "writeTextFile": False},
                "terminal": False,
                "_meta": {"parameterizedModelPicker": True},
            },
        }, time.monotonic() + timeout, timeout)

    def _read_stdout(self) -> None:
        try:
            for line in self.proc.stdout:
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if isinstance(message, dict):
                    self._messages.put(message)
        except (OSError, ValueError):
            pass
        finally:
            self._messages.put(None)

    def _read_stderr(self) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            while chunk := os.read(self.proc.stderr.fileno(), 4096):
                with self._stderr_lock:
                    self._stderr_at = time.monotonic()
                    self._stderr += decoder.decode(chunk)
                    if len(self._stderr) > STDERR_LIMIT:
                        half = STDERR_LIMIT // 2
                        self._stderr = self._stderr[:half] + self._stderr[-half:]
        except (OSError, ValueError):
            pass

    def clear_stderr(self) -> None:
        with self._stderr_lock:
            self._stderr = ""

    def stderr(self) -> str:
        """오류 응답보다 늦게 읽히는 stderr를 위해 0.1초 동안 새 내용이 없을 때까지(최대 1초) 기다린다."""
        start = time.monotonic()
        while True:
            with self._stderr_lock:
                quiet_at = max(start, self._stderr_at) + 0.1
            now = time.monotonic()
            if now >= min(quiet_at, start + 1):
                break
            time.sleep(0.01)
        with self._stderr_lock:
            return self._stderr

    def _ended(self) -> AcpError:
        return AcpError("Cursor ACP 서버가 끝났다")

    def _send(self, message: dict, deadline: float, timeout: float) -> None:
        data = memoryview((json.dumps({"jsonrpc": "2.0", **message}, ensure_ascii=False) + "\n").encode("utf-8"))
        try:
            while data:
                if self._closed or self.proc.poll() is not None:
                    raise self._ended()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AcpTimeout(f"시간 초과({timeout}초)")
                fd = self.proc.stdin.fileno()
                if select.select([], [fd], [], min(remaining, 0.1))[1]:
                    try:
                        data = data[os.write(fd, data):]
                    except BlockingIOError:
                        continue
        except (OSError, ValueError) as e:
            raise self._ended() from e

    def _reply(self, message: dict, deadline: float, timeout: float) -> None:
        if message["method"] == "session/request_permission":
            options = message.get("params", {}).get("options", [])
            reject = next((o for kind in ("reject_once", "reject_always") for o in options if o.get("kind") == kind), None)
            outcome = {"outcome": "selected", "optionId": reject["optionId"]} if reject else {"outcome": "cancelled"}
            response = {"result": {"outcome": outcome}}
        else:
            response = {"error": {"code": -32601, "message": "지원하지 않는다"}}
        self._send({"id": message["id"], **response}, deadline, timeout)

    def request(self, method: str, params: dict, deadline: float, timeout: float) -> tuple[dict, str]:
        """요청 하나를 보내고 결과와(session/prompt면) 답 텍스트를 돌려 준다. 도구 호출 앞 문장은 버린다."""
        self._next_id += 1
        request_id = self._next_id
        self._send({"id": request_id, "method": method, "params": params}, deadline, timeout)
        chunks: list[str] = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AcpTimeout(f"시간 초과({timeout}초)")
            try:
                message = self._messages.get(timeout=min(remaining, 0.1))
            except queue.Empty:
                if self._closed or self.proc.poll() is not None:
                    raise self._ended()
                continue
            if message is None:
                raise self._ended()
            if "method" in message and "id" in message:
                self._reply(message, deadline, timeout)
            elif message.get("method") == "session/update":
                update_params = message.get("params", {})
                if method != "session/prompt" or update_params.get("sessionId") != params.get("sessionId"):
                    continue
                update = update_params.get("update", {})
                kind = update.get("sessionUpdate")
                if kind == "agent_message_chunk":
                    chunks.append(str(update.get("content", {}).get("text", "")))
                elif kind == "tool_call":
                    chunks.clear()
            elif message.get("id") == request_id:
                if "error" in message:
                    error = message["error"]
                    raise AcpError(f"Cursor ACP 오류: {error.get('message', '') if isinstance(error, dict) else error}")
                result = message.get("result")
                if not isinstance(result, dict):
                    raise AcpError("Cursor ACP 응답 형식이 잘못되었다")
                return result, "".join(chunks)

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._messages.put(None)
            try:
                if self.proc is not None:
                    self._stop()
            finally:
                if self.root is not None:
                    _remove(self.root)
                with _LIVE_LOCK:
                    _LIVE.discard(self)

    def _stop(self) -> None:
        """stdin을 닫고 프로세스 그룹을 SIGTERM, SIGKILL 순으로 끝낸 뒤 그룹이 사라질 때까지 기다린다."""
        pgid = self.proc.pid
        try:
            self.proc.stdin.close()
        except (OSError, ValueError):
            pass
        for sig, wait in ((signal.SIGTERM, STOP_GRACE), (signal.SIGKILL, 1)):
            try:
                os.killpg(pgid, sig)
            except OSError:
                pass
            try:
                self.proc.wait(timeout=wait)
            except subprocess.TimeoutExpired:
                pass
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                os.killpg(pgid, 0)
            except OSError:
                break
            time.sleep(0.02)
        for thread in self._threads:
            thread.join(timeout=0.5)
        for stream in (self.proc.stdout, self.proc.stderr):
            try:
                stream.close()
            except (OSError, ValueError):
                pass


def _remove(root: Path) -> None:
    deadline = time.monotonic() + 2.5
    while True:
        try:
            shutil.rmtree(root)
            return
        except FileNotFoundError:
            return
        except OSError:
            if time.monotonic() >= deadline:
                return
            time.sleep(0.05)


class CursorAcpPool:
    """서버를 max_servers개까지 띄워 같은 작업 폴더의 작업에 돌려 쓴다. 작업마다 새 세션을 열고, 서버는
    sessions_per_server마다 바꾼다. 다 차면 다른 작업 폴더의 쉬는 서버를 닫고 새로 띄운다."""

    def __init__(self, exe: str, env_factory: Callable[[Path, Path], dict], max_servers: int, timeout: float,
                 sessions_per_server: int = SESSIONS_PER_SERVER, start_timeout: float = START_TIMEOUT):
        self.exe = exe
        self.env_factory = env_factory
        self.max_servers = max_servers
        self.timeout = timeout
        self.sessions_per_server = sessions_per_server
        self.start_timeout = start_timeout
        self._condition = threading.Condition()
        self._servers: set[_Server] = set()
        self._idle: list[_Server] = []
        self._closed = False

    def _acquire(self, worktree: Path) -> _Server:
        stale = None
        with self._condition:
            while True:
                if self._closed:
                    raise AcpError("Cursor ACP 실행기를 닫았다")
                same = next((s for s in self._idle if s.worktree == worktree), None)
                if same is not None:
                    self._idle.remove(same)
                    return same
                if len(self._servers) >= self.max_servers and self._idle:
                    stale = self._idle.pop(0)
                    self._servers.discard(stale)
                if len(self._servers) < self.max_servers:
                    server = _Server(self.exe, self.env_factory, worktree)
                    self._servers.add(server)
                    break
                self._condition.wait()
        if stale is not None:
            stale.close()
        return server

    def _release(self, server: _Server, reusable: bool) -> None:
        with self._condition:
            if reusable and not self._closed and server.sessions < self.sessions_per_server:
                self._idle.append(server)
                self._condition.notify()
                return
            self._servers.discard(server)
        server.close()
        with self._condition:
            self._condition.notify_all()

    def _open(self, server: _Server, worktree: Path, model: str, deadline: float, mcp_servers: list | None = None) -> str:
        result, _ = server.request("session/new", {"cwd": str(worktree), "mcpServers": mcp_servers or []},
                                   deadline, self.timeout)
        session = result.get("sessionId")
        if not isinstance(session, str) or not session:
            raise AcpError("Cursor ACP 세션 ID가 없다")
        options = result.get("configOptions") or []
        models = next((o.get("options", []) for o in options if o.get("id") == "model"), [])
        if model not in {o.get("value") for o in models}:
            raise AcpError(f"Cursor ACP에 모델이 없다: {model}")
        desired = {"model": model, **SETTINGS}
        for key, value in desired.items():
            if {o.get("id"): o.get("currentValue") for o in options}.get(key) != value:
                result, _ = server.request("session/set_config_option",
                                           {"sessionId": session, "configId": key, "value": value}, deadline, self.timeout)
                options = result.get("configOptions") or []
        current = {o.get("id"): o.get("currentValue") for o in options}
        mismatches = [f"{key}: 원하는 값 {value}, 실제 값 {current.get(key)}" for key, value in desired.items()
                      if current.get(key) != value]
        if mismatches:
            raise AcpError(f"Cursor ACP 설정이 다르다: {'; '.join(mismatches)}")
        return session

    def _prompt(self, server: _Server, session: str, text: str, deadline: float) -> str:
        result, answer = server.request("session/prompt", {"sessionId": session, "prompt": [{"type": "text", "text": text}]},
                                        deadline, self.timeout)
        if result.get("stopReason") != "end_turn":
            raise AcpError(f"Cursor 종료 사유 {result.get('stopReason')}")
        return answer

    def ask(self, worktree: Path, model: str, prompt: str, retry_prompt: str, parse: Callable[[str], dict | None]) -> dict | None:
        """prompt를 보내 parse가 받아들이면 그 결과를, 아니면 같은 세션에 retry_prompt를 한 번 더 보낸 결과를 돌려 준다."""
        if model.lower().startswith("auto"):
            raise AcpError(f"Cursor ACP에서 Auto 모델은 쓰지 않는다: {model}")
        worktree = Path(worktree)
        server = self._acquire(worktree)
        reusable = False
        sent = [prompt, retry_prompt]
        try:
            server.clear_stderr()
            server.start(self.start_timeout)
            deadline = time.monotonic() + self.timeout
            session = self._open(server, worktree, model, deadline)
            output = parse(self._prompt(server, session, prompt, deadline))
            if output is None:
                output = parse(self._prompt(server, session, retry_prompt, deadline))
            server.sessions += 1
            reusable = True
            return output
        except AcpError as e:
            stderr = _scrub(server.stderr(), sent).strip()
            message = _scrub(str(e), sent).strip()
            raise type(e)(f"{message}: {stderr}" if stderr else message) from e
        finally:
            self._release(server, reusable)

    def close(self) -> None:
        with self._condition:
            self._closed = True
            servers = list(self._servers)
            self._servers.clear()
            self._idle.clear()
            self._condition.notify_all()
        for server in servers:
            server.close()
