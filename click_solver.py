"""
click_solver.py — Универсальный click-based solver для ARC сред.

Стратегия: систематически кликает все позиции в grid. Когда что-то меняется
в frame (или levels_completed увеличивается), запоминает «горячую» зону.

Работает на любой click/keyboard_click игре без знания исходника.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env", override=True)


def _venv_python():
    for p in [
        PROJECT_ROOT / ".venv_arc" / "Scripts" / "python.exe",
        PROJECT_ROOT.parent / ".venv_arc" / "Scripts" / "python.exe",
    ]:
        if p.exists():
            return str(p)
    raise FileNotFoundError(".venv_arc not found")


CLICK_RUNNER = r'''
import json
import os
import sys
import arc_agi
from arcengine import GameAction
import numpy as np

env_id = sys.argv[1]
max_steps = int(sys.argv[2]) if len(sys.argv) > 2 else 500
strategy = sys.argv[3] if len(sys.argv) > 3 else "grid_sweep"
# strategy: grid_sweep (probe every cell), random (random clicks), keyboard_then_click

api_key = os.getenv("ARC_API_KEY", "").strip()
if not api_key:
    print(json.dumps({"ok": False, "error": "ARC_API_KEY not set"}))
    sys.exit(2)

try:
    arc = arc_agi.Arcade()
    env = arc.make(env_id)

    ACT = {1: GameAction.ACTION1, 2: GameAction.ACTION2, 3: GameAction.ACTION3,
           4: GameAction.ACTION4, 5: GameAction.ACTION5, 6: GameAction.ACTION6}

    played = 0
    last_levels = 0
    log = []

    if strategy == "grid_sweep":
        # Sweep all grid cells via ACTION6 click
        # Display 64x64, sweep every 4 pixels (16x16 grid)
        coords = [(x, y) for y in range(2, 64, 4) for x in range(2, 64, 4)]
        # Also try every 8 pixels (8x8 grid) for coarse sweep first
        coarse = [(x, y) for y in range(4, 64, 8) for x in range(4, 64, 8)]
        all_coords = coarse + coords

        for cx, cy in all_coords:
            if played >= max_steps:
                break
            fd = env.step(GameAction.ACTION6, data={"x": cx, "y": cy})
            played += 1
            if fd is None:
                continue
            lc = fd.levels_completed if isinstance(fd.levels_completed, int) else (1 if fd.levels_completed else 0)
            if lc > last_levels:
                log.append({"action": played, "click": [cx, cy], "level_up": lc})
                last_levels = lc
            if fd.state.name in ("WIN", "GAME_OVER"):
                break
    elif strategy == "keyboard_then_click":
        # Try keyboard actions (1-4) first, then click
        for round_i in range(max_steps // 10):
            if played >= max_steps:
                break
            for act_id in [1, 2, 3, 4]:
                fd = env.step(ACT[act_id])
                played += 1
                if fd is None or fd.state.name in ("WIN", "GAME_OVER"):
                    break
                lc = fd.levels_completed if isinstance(fd.levels_completed, int) else 0
                if lc > last_levels:
                    log.append({"action": played, "act": act_id, "level_up": lc})
                    last_levels = lc
            if fd and fd.state.name in ("WIN", "GAME_OVER"):
                break
    elif strategy == "random_click":
        import random
        random.seed(42)
        while played < max_steps:
            cx = random.randint(0, 63)
            cy = random.randint(0, 63)
            fd = env.step(GameAction.ACTION6, data={"x": cx, "y": cy})
            played += 1
            if fd is None:
                continue
            lc = fd.levels_completed if isinstance(fd.levels_completed, int) else 0
            if lc > last_levels:
                log.append({"action": played, "click": [cx, cy], "level_up": lc})
                last_levels = lc
            if fd.state.name in ("WIN", "GAME_OVER"):
                break

    sc = arc.get_scorecard()
    sc_dict = sc.model_dump() if hasattr(sc, "model_dump") else sc.dict()
    print(json.dumps({
        "ok": True,
        "scorecard": sc_dict,
        "log": log,
        "actions_used": played,
        "strategy": strategy,
    }, ensure_ascii=False))
except Exception as e:
    import traceback
    print(json.dumps({"ok": False, "error": str(e), "trace": traceback.format_exc()[:500]}))
'''


def solve_click_env(env_id: str, max_steps: int = 500, strategy: str = "grid_sweep"):
    """Решает click-based ARC среду."""
    import subprocess
    import json
    py = _venv_python()
    proc = subprocess.run(
        [py, "-c", CLICK_RUNNER, env_id, str(max_steps), strategy],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONUTF8": "1"},
        timeout=600,
    )
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if not stdout:
        return {"ok": False, "error": stderr or f"exit={proc.returncode}"}
    try:
        return json.loads(stdout.splitlines()[-1])
    except Exception:
        return {"ok": False, "raw": stdout, "stderr": stderr[:200]}


if __name__ == "__main__":
    env_id = sys.argv[1] if len(sys.argv) > 1 else "ft09-0d8bbf25"
    strategy = sys.argv[2] if len(sys.argv) > 2 else "grid_sweep"
    max_steps = int(sys.argv[3]) if len(sys.argv) > 3 else 500

    print(f"=== Click solver: {env_id}, strategy={strategy}, max_steps={max_steps} ===\n")
    t0 = time.time()
    r = solve_click_env(env_id, max_steps, strategy)
    dt = time.time() - t0
    print(f"Duration: {dt:.1f}s\n")

    if not r.get("ok"):
        print(f"ERROR: {r.get('error', r)}")
        sys.exit(1)

    sc = r.get("scorecard", {})
    print(f"card_id: {sc.get('card_id')}")
    print(f"score: {sc.get('score', 0):.4f}")
    print(f"levels: {sc.get('total_levels_completed')}/{sc.get('total_levels', '?')}")
    print(f"actions_used: {r.get('actions_used')}")
    runs = sc.get("environments", [{}])[0].get("runs", [{}])
    if runs:
        run = runs[0]
        print(f"state: {run.get('state')}")
        print(f"level_actions: {run.get('level_actions')}")
    print(f"\nLog (level-up moments): {r.get('log')}")
    print(f"\nhttps://three.arcprize.org/scorecards/{sc.get('card_id', '?')}")
