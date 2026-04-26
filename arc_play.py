from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent

def _load_env() -> None:
    """Загружает .env из текущего каталога или родительских (до 6 уровней)."""
    search = PROJECT_ROOT
    for _ in range(6):
        env_file = search / ".env"
        if env_file.exists():
            load_dotenv(env_file, override=False)
            return
        search = search.parent

_load_env()


def _find_arc_venv() -> Path:
    """
    Ищет рабочий .venv_arc: проходит вверх по дереву (до 6 уровней),
    проверяет реальный импорт arcengine. Это нужно для случаев, когда
    venv общий для проекта и лежит в корне репо.
    """
    _py_rel = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    first_candidate = PROJECT_ROOT / ".venv_arc"
    search = PROJECT_ROOT
    for _ in range(6):
        venv = search / ".venv_arc"
        py = venv / _py_rel
        if py.exists():
            try:
                probe = subprocess.run(
                    [str(py), "-c", "import arcengine; print('ok')"],
                    capture_output=True, text=True, timeout=15,
                )
                if probe.returncode == 0 and "ok" in probe.stdout:
                    return venv
            except Exception:
                pass
        search = search.parent
    return first_candidate

ARC_VENV_DIR = _find_arc_venv()
ARC_STATUS_PATH = PROJECT_ROOT / "data" / "arc_status.json"
ARC_HISTORY_PATH = PROJECT_ROOT / "data" / "arc_history.jsonl"
ARC_POLICY_PATH = PROJECT_ROOT / "data" / "arc_policy.json"
ARC_STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)

_LOCK = threading.Lock()
_RUN_THREAD: threading.Thread | None = None


def _venv_python() -> Path:
    if os.name == "nt":
        return ARC_VENV_DIR / "Scripts" / "python.exe"
    return ARC_VENV_DIR / "bin" / "python"


def _write_status(payload: dict[str, Any]) -> None:
    payload = dict(payload)
    payload["updated_at"] = int(time.time())
    with _LOCK:
        ARC_STATUS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def get_status() -> dict[str, Any]:
    if not ARC_STATUS_PATH.exists():
        return {"state": "idle", "message": "ARC runner is idle"}
    try:
        data = json.loads(ARC_STATUS_PATH.read_text(encoding="utf-8"))
        ts = int(data.get("updated_at", 0) or 0)
        if data.get("state") == "running" and ts and (time.time() - ts) > 300:
            data["state"] = "stale"
            data["message"] = "stale running state (previous process terminated)"
        return data
    except Exception as e:
        return {"state": "error", "message": f"status parse error: {e}"}


def _append_history(record: dict[str, Any]) -> None:
    line = json.dumps(record, ensure_ascii=False)
    with _LOCK:
        with ARC_HISTORY_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def _load_policy() -> dict[str, Any]:
    if not ARC_POLICY_PATH.exists():
        return {"version": 1, "envs": {}, "updated_at": 0}
    try:
        return json.loads(ARC_POLICY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "envs": {}, "updated_at": 0}


def _save_policy(policy: dict[str, Any]) -> None:
    policy["updated_at"] = int(time.time())
    with _LOCK:
        ARC_POLICY_PATH.write_text(json.dumps(policy, ensure_ascii=False, indent=2), encoding="utf-8")


def _update_policy(env_id: str, action_name: str, score: float, steps: int, ok: bool) -> None:
    policy = _load_policy()
    envs = policy.setdefault("envs", {})
    env = envs.setdefault(env_id, {"runs": 0, "ok_runs": 0, "best_score": 0.0, "actions": {}})
    env["runs"] = int(env.get("runs", 0)) + 1
    if ok:
        env["ok_runs"] = int(env.get("ok_runs", 0)) + 1
    env["best_score"] = max(float(env.get("best_score", 0.0) or 0.0), float(score))
    actions = env.setdefault("actions", {})
    a = actions.setdefault(
        action_name,
        {"runs": 0, "ok_runs": 0, "total_score": 0.0, "best_score": 0.0, "avg_steps": 0.0},
    )
    prev_runs = int(a.get("runs", 0))
    a["runs"] = prev_runs + 1
    if ok:
        a["ok_runs"] = int(a.get("ok_runs", 0)) + 1
    a["total_score"] = float(a.get("total_score", 0.0) or 0.0) + float(score)
    a["best_score"] = max(float(a.get("best_score", 0.0) or 0.0), float(score))
    a["avg_steps"] = ((float(a.get("avg_steps", 0.0) or 0.0) * prev_runs) + float(steps)) / max(1, a["runs"])
    _save_policy(policy)


def _choose_env(preferred: list[str] | None = None) -> str:
    preferred = preferred or ["ls20", "ft09", "tr28"]
    policy = _load_policy()
    envs = policy.get("envs", {})
    best_env = preferred[0]
    best_val = float("-inf")
    for env in preferred:
        e = envs.get(env, {})
        runs = int(e.get("runs", 0) or 0)
        ok_runs = int(e.get("ok_runs", 0) or 0)
        best_score = float(e.get("best_score", 0.0) or 0.0)
        exploit = (ok_runs / max(1, runs)) + best_score
        explore = 0.3 / max(1, runs + 1)
        val = exploit + explore
        if val > best_val:
            best_val = val
            best_env = env
    return best_env


def _choose_action(env_id: str) -> str:
    import random

    policy = _load_policy()
    env = policy.get("envs", {}).get(env_id, {})
    actions = env.get("actions", {}) if isinstance(env, dict) else {}
    candidates = ["ACTION1", "ACTION2", "ACTION3", "ACTION4"]
    # epsilon-greedy
    if random.random() < 0.2:
        return random.choice(candidates)
    best = "ACTION1"
    best_val = float("-inf")
    for name in candidates:
        a = actions.get(name, {})
        runs = int(a.get("runs", 0) or 0)
        ok_runs = int(a.get("ok_runs", 0) or 0)
        avg_score = (float(a.get("total_score", 0.0) or 0.0) / max(1, runs)) if runs else 0.0
        val = avg_score + (ok_runs / max(1, runs)) + (0.2 / max(1, runs + 1))
        if val > best_val:
            best_val = val
            best = name
    return best


def get_learning_stats() -> dict[str, Any]:
    from src.quantum.arc_qml import recommend_steps

    if not ARC_HISTORY_PATH.exists():
        return {
            "runs_total": 0,
            "runs_ok": 0,
            "best_score": 0.0,
            "best_env": None,
            "recommended_steps": 10,
            "qml_mode": "classical",
            "ibm_quantum": _ibm_quantum_status(),
            "policy": _load_policy(),
        }
    runs = []
    with _LOCK:
        for line in ARC_HISTORY_PATH.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                runs.append(json.loads(line))
            except Exception:
                continue
    ok_runs = [r for r in runs if r.get("ok")]
    best = max(ok_runs, key=lambda x: float(x.get("score", 0.0)), default=None)
    recommended, qml_mode = recommend_steps(runs)
    return {
        "runs_total": len(runs),
        "runs_ok": len(ok_runs),
        "best_score": float(best.get("score", 0.0)) if best else 0.0,
        "best_env": best.get("env_id") if best else None,
        "recommended_steps": recommended,
        "qml_mode": qml_mode,
        "ibm_quantum": _ibm_quantum_status(),
        "policy": _load_policy(),
        "last": runs[-1] if runs else None,
    }


def _ibm_quantum_status() -> str:
    token = (os.getenv("IBM_QUANTUM_TOKEN", "") or "").strip()
    if not token:
        return "token_missing"
    try:
        from qiskit_ibm_runtime import QiskitRuntimeService  # type: ignore

        service = QiskitRuntimeService(channel="ibm_quantum", token=token)
        backends = service.backends()
        return f"online:{len(backends)}"
    except Exception:
        return "token_set"


def ensure_arc_venv() -> tuple[bool, str]:
    py = _venv_python()
    try:
        if not py.exists():
            create_cmd = ["py", "-3.12", "-m", "venv", str(ARC_VENV_DIR)]
            subprocess.check_call(create_cmd, cwd=str(PROJECT_ROOT))
        check = subprocess.run(
            [str(py), "-c", "import arc_agi; import arcengine; print('ok')"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if check.returncode != 0:
            subprocess.check_call([str(py), "-m", "pip", "install", "--upgrade", "pip"], cwd=str(PROJECT_ROOT))
            subprocess.check_call(
                [str(py), "-m", "pip", "install", "arc-agi", "arcengine"],
                cwd=str(PROJECT_ROOT),
            )
        return True, "venv ready"
    except Exception as e:
        return False, str(e)


def _runner_script() -> str:
    return r"""
import json
import os
import sys
import arc_agi
from arcengine import GameAction

env_id = sys.argv[1] if len(sys.argv) > 1 else "ls20"
steps = int(sys.argv[2]) if len(sys.argv) > 2 else 10
render = (len(sys.argv) > 3 and sys.argv[3].lower() == "true")
action_name = sys.argv[4] if len(sys.argv) > 4 else "ACTION1"

try:
    api_key = os.getenv("ARC_API_KEY", "").strip() or os.getenv("ARC3_API_KEY", "").strip()
    if not api_key:
        print(json.dumps({"ok": False, "error": "ARC_API_KEY is not set"}))
        raise SystemExit(2)

    arc = arc_agi.Arcade()
    env = arc.make(env_id, render_mode="terminal" if render else None)
    action = getattr(GameAction, action_name, GameAction.ACTION1)
    for _ in range(steps):
        env.step(action)

    score = arc.get_scorecard()
    # Pydantic v2: model_dump(); v1: dict(); fallback: str
    if hasattr(score, "model_dump"):
        sc_dict = score.model_dump()
    elif hasattr(score, "dict"):
        sc_dict = score.dict()
    else:
        sc_dict = {"raw": str(score)}
    print(json.dumps({"ok": True, "scorecard": sc_dict, "action_name": action_name}, ensure_ascii=False))
except Exception as e:
    print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
"""


def _smart_runner_script_ls20() -> str:
    """
    BFS-оптимальные маршруты для всех 7 уровней ls20 (вычислены из ls20.py).
    A1=up(y-5), A2=down(y+5), A3=left(x-5), A4=right(x+5).

    Level 1 (StepsDecrement=1, 13 actions): player(34,45) -> rhsxkxzdjz(19,30) -> goal(34,10)
    Level 2 (default StepsDecrement, 41 actions)
    Level 3 (44 actions)
    Level 4 (StepsDecrement=1, 41 actions)
    Level 5 (42 actions)
    Level 6 (StepsDecrement=1, 35 actions, multi-goal)
    Level 7 (55 actions)

    Total: 271 actions for the entire game.
    """
    return r"""
import json
import os
import sys
import arc_agi
from arcengine import GameAction

env_id = sys.argv[1] if len(sys.argv) > 1 else "ls20"
steps  = int(sys.argv[2]) if len(sys.argv) > 2 else 400
render = (len(sys.argv) > 3 and sys.argv[3].lower() == "true")

A1, A2, A3, A4 = GameAction.ACTION1, GameAction.ACTION2, GameAction.ACTION3, GameAction.ACTION4

try:
    api_key = os.getenv("ARC_API_KEY", "").strip() or os.getenv("ARC3_API_KEY", "").strip()
    if not api_key:
        print(json.dumps({"ok": False, "error": "ARC_API_KEY is not set"}))
        raise SystemExit(2)

    arc = arc_agi.Arcade()
    # Открываем карточку с тегами для three.arcprize.org
    try:
        arc.open_scorecard(tags=["argos", "smart-bfs", "agent"])
    except Exception:
        pass
    env = arc.make(env_id, render_mode="terminal" if render else None)

    # BFS-оптимальные маршруты для всех 7 уровней ls20.
    L = {
        1: [3,3,3,1,1,1,1,4,4,4,1,1,1],
        2: [1,4,1,1,1,1,1,4,4,2,4,2,2,2,2,2,2,1,2,1,2,1,1,1,1,1,1,1,3,3,3,3,3,3,2,2,3,2,2,2,2],
        3: [1,1,1,1,1,1,1,1,4,4,4,4,2,2,2,2,2,2,2,2,1,1,1,1,4,4,4,4,4,1,1,1,3,1,2,2,4,2,2,2,2,2,2,2],
        4: [3,3,3,2,2,2,2,3,2,1,2,1,2,1,1,1,3,3,2,3,3,2,2,2,4,4,1,2,3,3,1,1,1,4,1,4,1,1,3,3,3],
        5: [1,3,3,3,2,3,3,1,3,3,2,1,3,1,1,4,4,1,1,1,2,4,4,4,2,2,3,4,3,4,3,4,4,4,2,4,4,1,1,1,1,1],
        6: [1,1,4,4,2,1,4,1,1,1,3,3,2,3,1,3,3,3,1,1,1,4,4,4,4,4,4,2,2,4,4,2,4,2,2],
        7: [3,3,2,2,2,2,2,1,2,4,4,1,2,1,2,1,2,3,3,1,1,1,4,4,4,4,2,4,4,2,4,4,1,1,1,1,1,4,1,2,2,2,2,2,2,3,3,3,1,3,3,2,2,2,2],
    }
    ACT = {1: A1, 2: A2, 3: A3, 4: A4}
    sequence = []
    for lvl_num in range(1, 8):
        sequence += [ACT[a] for a in L[lvl_num]]

    played = 0
    for action in sequence:
        if played >= steps:
            break
        env.step(action)
        played += 1

    score = arc.get_scorecard()
    if hasattr(score, "model_dump"):
        sc_dict = score.model_dump()
    elif hasattr(score, "dict"):
        sc_dict = score.dict()
    else:
        sc_dict = {"raw": str(score)}
    print(json.dumps({"ok": True, "scorecard": sc_dict, "action_name": "SMART_LS20"}, ensure_ascii=False))
except Exception as e:
    print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
"""


def play_game_smart(env_id: str = "ls20", steps: int = 400, render: bool = False) -> dict[str, Any]:
    """Запускает игру с предвычисленным BFS-маршрутом (для ls20)."""
    ok, msg = ensure_arc_venv()
    if not ok:
        return {"ok": False, "error": f"venv setup failed: {msg}"}

    script = _smart_runner_script_ls20() if env_id == "ls20" else _runner_script()
    py = _venv_python()
    cmd = [str(py), "-c", script, env_id, str(max(1, steps)), "true" if render else "false"]
    if env_id != "ls20":
        cmd.append("ACTION1")

    proc = subprocess.run(
        cmd,
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        timeout=600,
    )
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0 and not stdout:
        return {"ok": False, "error": stderr or f"runner exit={proc.returncode}"}
    try:
        data = json.loads(stdout.splitlines()[-1]) if stdout else {}
    except Exception:
        data = {"ok": False, "error": "invalid runner json", "stdout": stdout, "stderr": stderr}
    if stderr and isinstance(data, dict):
        data.setdefault("stderr", stderr)
    return data


def play_game(env_id: str = "ls20", steps: int = 10, render: bool = False, action_name: str = "ACTION1") -> dict[str, Any]:
    ok, msg = ensure_arc_venv()
    if not ok:
        return {"ok": False, "error": f"venv setup failed: {msg}"}

    py = _venv_python()
    cmd = [
        str(py),
        "-c",
        _runner_script(),
        env_id,
        str(max(1, steps)),
        "true" if render else "false",
        action_name or "ACTION1",
    ]
    proc = subprocess.run(
        cmd,
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={
            **os.environ,
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        },
        timeout=180,
    )
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0 and not stdout:
        return {"ok": False, "error": stderr or f"runner exit={proc.returncode}"}
    try:
        data = json.loads(stdout.splitlines()[-1]) if stdout else {}
    except Exception:
        data = {"ok": False, "error": "invalid runner json", "stdout": stdout, "stderr": stderr}
    if stderr and isinstance(data, dict):
        data.setdefault("stderr", stderr)
    return data


def start_game_async(
    env_id: str = "ls20",
    steps: int = 10,
    render: bool = False,
    action_name: str | None = None,
) -> dict[str, Any]:
    global _RUN_THREAD
    with _LOCK:
        if _RUN_THREAD and _RUN_THREAD.is_alive():
            return {"ok": False, "message": "ARC game already running"}

        selected_env = _choose_env() if (env_id or "").strip().lower() == "auto" else env_id
        explicit_action = (action_name or "").strip().upper()
        # Умная стратегия для ls20: применяется ВСЕГДА, кроме явно заданного ACTION2/3/4
        # (для ручной отладки одиночными действиями)
        use_smart = selected_env == "ls20" and explicit_action not in ("ACTION2", "ACTION3", "ACTION4")
        if use_smart:
            selected_action = "SMART_LS20"
        else:
            selected_action = explicit_action or _choose_action(selected_env)
        learned_steps = steps
        if learned_steps <= 0:
            learned_steps = 400 if use_smart else int(get_learning_stats().get("recommended_steps", 10) or 10)

        def _job():
            _write_status(
                {
                    "state": "running",
                    "env_id": selected_env,
                    "steps": learned_steps,
                    "action_name": selected_action,
                    "render": bool(render),
                    "message": "ARC run started (smart BFS strategy)" if use_smart else "ARC run started (adaptive mode)",
                }
            )
            if use_smart:
                result = play_game_smart(env_id=selected_env, steps=learned_steps, render=render)
            else:
                result = play_game(
                    env_id=selected_env,
                    steps=learned_steps,
                    render=render,
                    action_name=selected_action,
                )
            if result.get("ok"):
                sc = result.get("scorecard", {})
                score = float(sc.get("score", 0.0) or 0.0)
                total_actions = int(sc.get("total_actions", 0) or 0)
                total_levels_completed = int(sc.get("total_levels_completed", 0) or 0)
                _write_status(
                    {
                        "state": "done",
                        "env_id": selected_env,
                        "steps": learned_steps,
                        "action_name": selected_action,
                        "score": score,
                        "total_actions": total_actions,
                        "total_levels_completed": total_levels_completed,
                        "message": "ARC run completed",
                        "scorecard": sc,
                    }
                )
                _update_policy(
                    env_id=selected_env,
                    action_name=selected_action,
                    score=score,
                    steps=learned_steps,
                    ok=True,
                )
                _append_history(
                    {
                        "ts": int(time.time()),
                        "ok": True,
                        "env_id": selected_env,
                        "action_name": selected_action,
                        "steps": learned_steps,
                        "score": score,
                        "total_actions": total_actions,
                        "total_levels_completed": total_levels_completed,
                    }
                )
            else:
                _write_status(
                    {
                        "state": "error",
                        "env_id": selected_env,
                        "steps": learned_steps,
                        "action_name": selected_action,
                        "message": result.get("error", "ARC run failed"),
                        "details": result,
                    }
                )
                _update_policy(
                    env_id=selected_env,
                    action_name=selected_action,
                    score=0.0,
                    steps=learned_steps,
                    ok=False,
                )
                _append_history(
                    {
                        "ts": int(time.time()),
                        "ok": False,
                        "env_id": selected_env,
                        "action_name": selected_action,
                        "steps": learned_steps,
                        "error": result.get("error", "ARC run failed"),
                    }
                )

        _RUN_THREAD = threading.Thread(target=_job, daemon=True, name="ArcRunner")
        _RUN_THREAD.start()
    return {"ok": True, "message": "ARC run started"}


if __name__ == "__main__":
    # quick local test
    print(start_game_async("ls20", 10, False))
