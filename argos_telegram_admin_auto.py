#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def load_dotenv_file(path: Path) -> None:
    if not path.exists():
        return
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export ") :].strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key:
                continue
            if ((value.startswith('"') and value.endswith('"')) or
                    (value.startswith("'") and value.endswith("'"))):
                value = value[1:-1]
            os.environ.setdefault(key, value)
    except Exception:
        pass


PROJECT_ROOT = Path(os.getcwd()).resolve()
load_dotenv_file(PROJECT_ROOT / ".env")
load_dotenv_file(PROJECT_ROOT / ".env.local")
load_dotenv_file(PROJECT_ROOT / ".env.production")


def now_ts() -> float:
    return time.time()


def utc_iso(ts: Optional[float] = None) -> str:
    import datetime as dt
    if ts is None:
        ts = now_ts()
    return dt.datetime.utcfromtimestamp(ts).isoformat() + "Z"


def read_text(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return default


def write_text(path: Path, data: str) -> None:
    path.write_text(data, encoding="utf-8")


def append_jsonl(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(read_text(path, ""))
    except Exception:
        return default


def save_json(path: Path, obj: Any) -> None:
    write_text(path, json.dumps(obj, ensure_ascii=False, indent=2))


def truncate(text: str, limit: int = 3500) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def detect_venv_python(root: Path) -> str:
    candidates = [
        root / ".venv" / "Scripts" / "python.exe",
        root / ".venv" / "bin" / "python",
        root / "venv" / "Scripts" / "python.exe",
        root / "venv" / "bin" / "python",
        root / "env" / "Scripts" / "python.exe",
        root / "env" / "bin" / "python",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return sys.executable


def detect_entrypoint(root: Path) -> Tuple[str, str]:
    explicit_cmd = os.getenv("ARGOS_CMD", "").strip()
    if explicit_cmd:
        return "command", explicit_cmd

    file_candidates = [
        "main.py",
        "app.py",
        "run.py",
        "bot.py",
        "server.py",
        "src/main.py",
        "src/app.py",
        "src/run.py",
        "core.py",
        "src/core.py",
    ]
    for rel in file_candidates:
        p = root / rel
        if p.exists():
            return "file", rel

    module_candidates = [
        "main",
        "app",
        "run",
        "src.main",
        "src.app",
        "src.run",
    ]
    for mod in module_candidates:
        return "module", mod

    return "file", "main.py"


def build_run_command(root: Path) -> str:
    mode, target = detect_entrypoint(root)
    if mode == "command":
        return target
    python_bin = detect_venv_python(root)
    if mode == "file":
        return f'"{python_bin}" "{root / target}"'
    if mode == "module":
        return f'"{python_bin}" -m {target}'
    return f'"{python_bin}" "{root / "main.py"}"'


BOT_TOKEN = (
    os.getenv("TG_BOT_TOKEN", "").strip()
    or os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
)
ADMIN_IDS = {
    int(x.strip())
    for x in (
        os.getenv("TG_ADMIN_IDS", "")
        or os.getenv("TELEGRAM_ADMIN_IDS", "")
    ).split(",")
    if x.strip().isdigit()
}

PROJECT_CWD = Path(os.getenv("ARGOS_CWD", os.getcwd())).resolve()
ARGOS_CMD = build_run_command(PROJECT_CWD)

LOG_FILE = PROJECT_CWD / os.getenv("ARGOS_LOG_FILE", "argos_runtime.log")
STATE_FILE = PROJECT_CWD / os.getenv("ARGOS_STATE_FILE", "argos_admin_state.json")
QUEUE_FILE = PROJECT_CWD / os.getenv("ARGOS_QUEUE_FILE", "admin_queue.jsonl")
RESULTS_FILE = PROJECT_CWD / os.getenv("ARGOS_RESULTS_FILE", "admin_results.jsonl")
PID_FILE = PROJECT_CWD / "argos_runtime.pid"
OFFSET_FILE = PROJECT_CWD / ".telegram_offset"
RESULTS_POS_FILE = PROJECT_CWD / ".admin_results.pos"

POLL_TIMEOUT = 30
POLL_SLEEP = 1.5
MAX_MESSAGE_LEN = 3500


class TelegramAPI:
    def __init__(self, token: str) -> None:
        self.base = f"https://api.telegram.org/bot{token}"

    def _request(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base}/{method}"
        data = urllib.parse.urlencode(params).encode("utf-8")
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        obj = json.loads(raw)
        if not obj.get("ok"):
            raise RuntimeError(f"Telegram API error: {obj}")
        return obj

    def get_updates(self, offset: Optional[int] = None, timeout: int = 30) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        return self._request("getUpdates", params).get("result", [])

    def send_message(self, chat_id: int, text: str) -> None:
        if len(text) <= MAX_MESSAGE_LEN:
            self._request("sendMessage", {"chat_id": chat_id, "text": text})
            return
        chunks = [text[i : i + MAX_MESSAGE_LEN] for i in range(0, len(text), MAX_MESSAGE_LEN)]
        for chunk in chunks:
            self._request("sendMessage", {"chat_id": chat_id, "text": chunk})

    def answer(self, chat_id: int, text: str) -> None:
        try:
            self.send_message(chat_id, text)
        except Exception as e:
            print(f"[telegram] send error: {e}", file=sys.stderr)


@dataclass
class RuntimeState:
    running: bool = False
    pid: Optional[int] = None
    started_at: Optional[float] = None
    last_exit_code: Optional[int] = None
    router_mode: str = "auto"
    llm_fallback: bool = True
    last_error: Optional[str] = None
    command: str = ARGOS_CMD
    cwd: str = str(PROJECT_CWD)

    def uptime_s(self) -> Optional[int]:
        if self.running and self.started_at:
            return int(now_ts() - self.started_at)
        return None


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.state = self._load()

    def _load(self) -> RuntimeState:
        data = load_json(self.path, None)
        if isinstance(data, dict):
            try:
                return RuntimeState(**data)
            except Exception:
                pass
        return RuntimeState()

    def save(self) -> None:
        with self.lock:
            save_json(self.path, asdict(self.state))

    def update(self, **kwargs: Any) -> None:
        with self.lock:
            for k, v in kwargs.items():
                setattr(self.state, k, v)
            save_json(self.path, asdict(self.state))

    def get(self) -> RuntimeState:
        with self.lock:
            return RuntimeState(**asdict(self.state))


class ProcessManager:
    def __init__(self, state_store: StateStore, cwd: Path, cmd: str, log_file: Path) -> None:
        self.state_store = state_store
        self.cwd = cwd
        self.cmd = cmd
        self.log_file = log_file
        self.proc: Optional[subprocess.Popen] = None
        self.lock = threading.Lock()
        self._restore_if_possible()

    def _restore_if_possible(self) -> None:
        pid_text = read_text(PID_FILE, "").strip()
        if not pid_text.isdigit():
            return
        pid = int(pid_text)
        if self._pid_exists(pid):
            self.state_store.update(running=True, pid=pid)
        else:
            try:
                PID_FILE.unlink(missing_ok=True)
            except Exception:
                pass

    @staticmethod
    def _pid_exists(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def start(self) -> str:
        with self.lock:
            current = self.state_store.get()
            if current.running and current.pid and self._pid_exists(current.pid):
                return f"Уже запущен. PID={current.pid}"

            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            logf = self.log_file.open("a", encoding="utf-8")

            creationflags = 0
            preexec_fn = None
            if os.name == "nt":
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
            else:
                preexec_fn = os.setsid

            self.proc = subprocess.Popen(
                shlex.split(self.cmd),
                cwd=str(self.cwd),
                stdout=logf,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=creationflags,
                preexec_fn=preexec_fn,
                env=os.environ.copy(),
            )
            PID_FILE.write_text(str(self.proc.pid), encoding="utf-8")
            self.state_store.update(
                running=True,
                pid=self.proc.pid,
                started_at=now_ts(),
                last_error=None,
                command=self.cmd,
                cwd=str(self.cwd),
            )
            return f"Запущен. PID={self.proc.pid}\ncmd={self.cmd}"

    def stop(self) -> str:
        with self.lock:
            state = self.state_store.get()
            pid = state.pid
            if not pid:
                self.state_store.update(running=False, pid=None)
                return "Процесс не найден."

            if not self._pid_exists(pid):
                self.state_store.update(running=False, pid=None)
                try:
                    PID_FILE.unlink(missing_ok=True)
                except Exception:
                    pass
                return "Процесс уже остановлен."

            try:
                if os.name == "nt":
                    try:
                        os.kill(pid, signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
                    except Exception:
                        pass
                    time.sleep(2)
                    if self._pid_exists(pid):
                        os.kill(pid, signal.SIGTERM)
                else:
                    os.killpg(os.getpgid(pid), signal.SIGTERM)

                deadline = time.time() + 10
                while time.time() < deadline:
                    if not self._pid_exists(pid):
                        break
                    time.sleep(0.4)

                if self._pid_exists(pid):
                    if os.name == "nt":
                        os.kill(pid, signal.SIGTERM)
                    else:
                        os.killpg(os.getpgid(pid), signal.SIGKILL)

                self.state_store.update(running=False, pid=None)
                try:
                    PID_FILE.unlink(missing_ok=True)
                except Exception:
                    pass
                return f"Остановлен. PID={pid}"
            except Exception as e:
                self.state_store.update(last_error=f"stop failed: {e}")
                return f"Ошибка остановки: {e}"

    def restart(self) -> str:
        return f"{self.stop()}\n{self.start()}"

    def status(self) -> str:
        s = self.state_store.get()
        alive = bool(s.pid and self._pid_exists(s.pid))
        if s.running and not alive:
            self.state_store.update(running=False, pid=None)
            s = self.state_store.get()

        lines = [
            "ARGOS STATUS",
            f"running: {s.running}",
            f"pid: {s.pid}",
            f"cwd: {s.cwd}",
            f"cmd: {s.command}",
            f"router_mode: {s.router_mode}",
            f"llm_fallback: {s.llm_fallback}",
            f"uptime_s: {s.uptime_s()}",
            f"log_file: {self.log_file.name}",
            f"queue_file: {QUEUE_FILE.name}",
            f"results_file: {RESULTS_FILE.name}",
            f"last_exit_code: {s.last_exit_code}",
            f"last_error: {s.last_error}",
        ]
        return "\n".join(lines)

    def tail_logs(self, n: int = 60) -> str:
        text = read_text(self.log_file, "")
        if not text:
            return "Лог пуст."
        return truncate("\n".join(text.splitlines()[-n:]))

    def autodetect_info(self) -> str:
        mode, target = detect_entrypoint(self.cwd)
        python_bin = detect_venv_python(self.cwd)
        return "\n".join([
            "AUTODETECT",
            f"python: {python_bin}",
            f"entry_mode: {mode}",
            f"entry_target: {target}",
            f"resolved_cmd: {self.cmd}",
        ])


class QueueBridge:
    def __init__(self, queue_file: Path, results_file: Path) -> None:
        self.queue_file = queue_file
        self.results_file = results_file
        self.queue_lock = threading.Lock()

    def send_command(
        self,
        action: str,
        args: Optional[Dict[str, Any]] = None,
        source: str = "telegram",
        chat_id: Optional[int] = None,
        user_id: Optional[int] = None,
        text: Optional[str] = None,
    ) -> str:
        cmd = {
            "id": f"{int(time.time() * 1000)}-{os.getpid()}",
            "ts": utc_iso(),
            "source": source,
            "chat_id": chat_id,
            "user_id": user_id,
            "action": action,
            "args": args or {},
            "text": text,
        }
        with self.queue_lock:
            append_jsonl(self.queue_file, cmd)
        return cmd["id"]

    def read_new_results(self) -> List[Dict[str, Any]]:
        pos = 0
        if RESULTS_POS_FILE.exists():
            txt = read_text(RESULTS_POS_FILE, "0").strip()
            if txt.isdigit():
                pos = int(txt)
        if not self.results_file.exists():
            return []

        results: List[Dict[str, Any]] = []
        with self.results_file.open("r", encoding="utf-8") as f:
            f.seek(pos)
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    results.append(json.loads(line))
                except Exception:
                    results.append({"text": line})
            new_pos = f.tell()
        write_text(RESULTS_POS_FILE, str(new_pos))
        return results


def parse_command(text: str) -> Tuple[str, Dict[str, Any]]:
    parts = text.strip().split(maxsplit=2)
    cmd = parts[0].lower()

    if cmd == "/ping":
        return "local_ping", {}
    if cmd == "/status":
        return "local_status", {}
    if cmd == "/logs":
        return "local_logs", {}
    if cmd == "/start":
        return "local_start", {}
    if cmd == "/stop":
        return "local_stop", {}
    if cmd == "/restart":
        return "local_restart", {}
    if cmd == "/autodetect":
        return "local_autodetect", {}
    if cmd == "/route":
        return "queue_route", {"text": text[len("/route"):].strip()}
    if cmd == "/router":
        mode = parts[1].strip().lower() if len(parts) > 1 else ""
        if mode not in {"strict", "auto"}:
            return "help", {}
        return "queue_router_mode", {"mode": mode}
    if cmd == "/fallback":
        mode = parts[1].strip().lower() if len(parts) > 1 else ""
        if mode not in {"on", "off"}:
            return "help", {}
        return "queue_fallback", {"enabled": mode == "on"}
    if cmd == "/skills":
        sub = parts[1].strip().lower() if len(parts) > 1 else ""
        if sub == "reload":
            return "queue_skills_reload", {}
        if sub == "list":
            return "queue_skills_list", {}
        return "help", {}
    if cmd == "/skill":
        if len(parts) < 3:
            return "help", {}
        sub = parts[1].strip().lower()
        name = parts[2].strip()
        if sub == "enable":
            return "queue_skill_enable", {"name": name}
        if sub == "disable":
            return "queue_skill_disable", {"name": name}
        return "help", {}
    if cmd == "/raw":
        payload = text[len("/raw"):].strip()
        try:
            obj = json.loads(payload)
            if not isinstance(obj, dict):
                raise ValueError("raw json must be object")
            return "queue_raw", obj
        except Exception as e:
            return "error", {"message": f"Неверный JSON: {e}"}
    return "help", {}


def help_text() -> str:
    return (
        "Команды:\n"
        "/start\n"
        "/stop\n"
        "/restart\n"
        "/status\n"
        "/logs\n"
        "/autodetect\n"
        "/ping\n"
        "/route <text>\n"
        "/router strict|auto\n"
        "/fallback on|off\n"
        "/skills reload\n"
        "/skills list\n"
        "/skill enable <name>\n"
        "/skill disable <name>\n"
        "/raw <json>"
    )


class ArgosTelegramAdmin:
    def __init__(self) -> None:
        if not BOT_TOKEN:
            raise RuntimeError("Не задан TG_BOT_TOKEN/TELEGRAM_BOT_TOKEN")
        if not ADMIN_IDS:
            raise RuntimeError("Не заданы TG_ADMIN_IDS/TELEGRAM_ADMIN_IDS")

        self.tg = TelegramAPI(BOT_TOKEN)
        self.state_store = StateStore(STATE_FILE)
        self.proc = ProcessManager(self.state_store, PROJECT_CWD, ARGOS_CMD, LOG_FILE)
        self.bridge = QueueBridge(QUEUE_FILE, RESULTS_FILE)
        self.stop_event = threading.Event()
        self.offset = self._load_offset()

    def _load_offset(self) -> Optional[int]:
        txt = read_text(OFFSET_FILE, "").strip()
        return int(txt) if txt.isdigit() else None

    def _save_offset(self, offset: int) -> None:
        write_text(OFFSET_FILE, str(offset))

    @staticmethod
    def is_admin(user_id: int) -> bool:
        return user_id in ADMIN_IDS

    def handle_local(self, action: str, args: Dict[str, Any]) -> str:
        if action == "local_ping":
            return "pong"
        if action == "local_status":
            return self.proc.status()
        if action == "local_logs":
            return self.proc.tail_logs()
        if action == "local_start":
            return self.proc.start()
        if action == "local_stop":
            return self.proc.stop()
        if action == "local_restart":
            return self.proc.restart()
        if action == "local_autodetect":
            return self.proc.autodetect_info()
        return "Unknown local action"

    def handle_queue(self, action: str, args: Dict[str, Any], chat_id: int, user_id: int, raw_text: str) -> str:
        if action == "queue_router_mode":
            mode = args["mode"]
            self.state_store.update(router_mode=mode)
            cmd_id = self.bridge.send_command("set_router_mode", {"mode": mode}, chat_id=chat_id, user_id=user_id, text=raw_text)
            return f"queued: set_router_mode\nmode={mode}\nid={cmd_id}"
        if action == "queue_fallback":
            enabled = bool(args["enabled"])
            self.state_store.update(llm_fallback=enabled)
            cmd_id = self.bridge.send_command("set_llm_fallback", {"enabled": enabled}, chat_id=chat_id, user_id=user_id, text=raw_text)
            return f"queued: set_llm_fallback\nenabled={enabled}\nid={cmd_id}"
        if action == "queue_skills_reload":
            cmd_id = self.bridge.send_command("reload_skills", {}, chat_id=chat_id, user_id=user_id, text=raw_text)
            return f"queued: reload_skills\nid={cmd_id}"
        if action == "queue_skills_list":
            cmd_id = self.bridge.send_command("list_skills", {}, chat_id=chat_id, user_id=user_id, text=raw_text)
            return f"queued: list_skills\nid={cmd_id}"
        if action == "queue_skill_enable":
            cmd_id = self.bridge.send_command("enable_skill", {"name": args["name"]}, chat_id=chat_id, user_id=user_id, text=raw_text)
            return f"queued: enable_skill\nname={args['name']}\nid={cmd_id}"
        if action == "queue_skill_disable":
            cmd_id = self.bridge.send_command("disable_skill", {"name": args["name"]}, chat_id=chat_id, user_id=user_id, text=raw_text)
            return f"queued: disable_skill\nname={args['name']}\nid={cmd_id}"
        if action == "queue_route":
            cmd_id = self.bridge.send_command("explain_route", {"text": args.get("text", "")}, chat_id=chat_id, user_id=user_id, text=raw_text)
            return f"queued: explain_route\nid={cmd_id}"
        if action == "queue_raw":
            obj = dict(args)
            action_name = obj.pop("action", None)
            if not action_name:
                return "В raw JSON нужен ключ action."
            cmd_id = self.bridge.send_command(action_name, obj, chat_id=chat_id, user_id=user_id, text=raw_text)
            return f"queued raw\nid={cmd_id}"
        return f"Неизвестная queued action: {action}"

    def on_message(self, msg: Dict[str, Any]) -> None:
        message = msg.get("message") or {}
        chat = message.get("chat") or {}
        from_user = message.get("from") or {}
        chat_id = chat.get("id")
        user_id = from_user.get("id")
        text = (message.get("text") or "").strip()
        if not chat_id or not user_id or not text:
            return
        if not self.is_admin(int(user_id)):
            # Бот состоит в общем Council-чате с другими entity-ботами; раньше
            # отвечал "Access denied." на КАЖДОЕ их сообщение (396 раз за 2 дня,
            # ~25% всего чата) — теперь молча игнорирует чужие сообщения.
            return
        try:
            action, args = parse_command(text)
            if action == "help":
                self.tg.answer(int(chat_id), help_text())
                return
            if action == "error":
                self.tg.answer(int(chat_id), args["message"])
                return
            if action.startswith("local_"):
                self.tg.answer(int(chat_id), truncate(self.handle_local(action, args)))
                return
            if action.startswith("queue_"):
                reply = self.handle_queue(action, args, int(chat_id), int(user_id), text)
                self.tg.answer(int(chat_id), truncate(reply))
                return
            self.tg.answer(int(chat_id), "Unknown command.")
        except Exception:
            err = traceback.format_exc()
            print(err, file=sys.stderr)
            self.tg.answer(int(chat_id), truncate(f"Ошибка:\n{err}", 3000))

    def telegram_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                updates = self.tg.get_updates(offset=self.offset, timeout=POLL_TIMEOUT)
                for upd in updates:
                    upd_id = int(upd.get("update_id", 0))
                    self.offset = upd_id + 1
                    self._save_offset(self.offset)
                    self.on_message(upd)
            except KeyboardInterrupt:
                return
            except Exception as e:
                print(f"[telegram] poll error: {e}", file=sys.stderr)
                time.sleep(POLL_SLEEP)

    def results_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                results = self.bridge.read_new_results()
                for item in results:
                    chat_id = item.get("chat_id")
                    text = item.get("text") or json.dumps(item, ensure_ascii=False, indent=2)
                    if chat_id:
                        self.tg.answer(int(chat_id), truncate(f"[runtime]\n{text}"))
            except Exception as e:
                print(f"[results] error: {e}", file=sys.stderr)
            time.sleep(1.0)

    def run(self) -> None:
        print("ARGOS Telegram Admin Auto started")
        print(f"cwd={PROJECT_CWD}")
        print(f"cmd={ARGOS_CMD}")
        print(f"admins={sorted(ADMIN_IDS)}")
        print(self.proc.autodetect_info())
        print(f"queue={QUEUE_FILE}")
        print(f"results={RESULTS_FILE}")

        t1 = threading.Thread(target=self.telegram_loop, daemon=True)
        t2 = threading.Thread(target=self.results_loop, daemon=True)
        t1.start()
        t2.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop_event.set()
            print("Stopping...")


RUNTIME_HOOK_DOC = r"""
Минимальный hook для ARGOS:

import json
from pathlib import Path

QUEUE_FILE = Path("admin_queue.jsonl")
RESULTS_FILE = Path("admin_results.jsonl")

def append_result(obj):
    with RESULTS_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")

def process_admin_queue(runtime):
    if not QUEUE_FILE.exists():
        return
    lines = QUEUE_FILE.read_text(encoding="utf-8").splitlines()
    if not lines:
        return
    QUEUE_FILE.write_text("", encoding="utf-8")
    for line in lines:
        cmd = None
        try:
            cmd = json.loads(line)
            action = cmd.get("action")
            args = cmd.get("args", {})
            chat_id = cmd.get("chat_id")

            if action == "set_router_mode":
                runtime.router.mode = args["mode"]
                append_result({"chat_id": chat_id, "text": f"router.mode={runtime.router.mode}"})
            elif action == "set_llm_fallback":
                runtime.router.llm_fallback = bool(args["enabled"])
                append_result({"chat_id": chat_id, "text": f"llm_fallback={runtime.router.llm_fallback}"})
            elif action == "reload_skills":
                runtime.skill_manager.reload_all()
                append_result({"chat_id": chat_id, "text": "skills reloaded"})
            elif action == "list_skills":
                names = runtime.skill_manager.list_skills()
                append_result({"chat_id": chat_id, "text": "skills:\n" + "\n".join(names)})
            elif action == "enable_skill":
                runtime.skill_manager.enable(args["name"])
                append_result({"chat_id": chat_id, "text": f"enabled {args['name']}"})
            elif action == "disable_skill":
                runtime.skill_manager.disable(args["name"])
                append_result({"chat_id": chat_id, "text": f"disabled {args['name']}"})
            elif action == "explain_route":
                result = runtime.router.explain(args.get("text", ""))
                append_result({"chat_id": chat_id, "text": str(result)})
            else:
                append_result({"chat_id": chat_id, "text": f"unknown action: {action}"})
        except Exception as e:
            append_result({"chat_id": (cmd or {}).get("chat_id"), "text": f"admin queue error: {e}"})

Вызывать process_admin_queue(runtime) раз в 0.5-1 сек.
"""


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--hook-doc":
        print(RUNTIME_HOOK_DOC)
        return
    app = ArgosTelegramAdmin()
    app.run()


if __name__ == "__main__":
    main()

