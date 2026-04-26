"""
train_arc.py — обучающие забеги ls20 через play_game_smart.

Каждый забег:
1. Вызывает play_game_smart() в subprocess (всегда свежий код)
2. Записывает результат в data/arc_history.jsonl (общая БД ARGOS)
3. Обновляет data/arc_policy.json (политика выбора действий)
4. Печатает сводку

Запускать:
  python train_arc.py [N_GAMES] [STEPS_PER_GAME]
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

# Каталог с arc_play.py и data/
PROJECT_ROOT = Path(__file__).resolve().parent.parent  # F:\debug\argoss\
sys.path.insert(0, str(PROJECT_ROOT))

import arc_play  # type: ignore

DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
HISTORY = DATA_DIR / "arc_history.jsonl"
POLICY  = DATA_DIR / "arc_policy.json"


def append_history(record: dict) -> None:
    """Дописывает запись в общий arc_history.jsonl."""
    with HISTORY.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def update_policy(env_id: str, action_name: str, score: float, steps: int, ok: bool) -> None:
    """Обновляет arc_policy.json — счётчик удачных runs по env+action."""
    if POLICY.exists():
        try:
            policy = json.loads(POLICY.read_text(encoding="utf-8"))
        except Exception:
            policy = {"version": 1, "envs": {}}
    else:
        policy = {"version": 1, "envs": {}}

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

    policy["updated_at"] = int(time.time())
    POLICY.write_text(json.dumps(policy, ensure_ascii=False, indent=2), encoding="utf-8")


def play_one(env_id: str, steps: int) -> dict:
    """Один забег + запись в БД."""
    t0 = time.time()
    r = arc_play.play_game_smart(env_id=env_id, steps=steps, render=False)
    dt = time.time() - t0
    sc = r.get("scorecard", {})
    score = float(sc.get("score", 0.0) or 0.0)
    total_actions = int(sc.get("total_actions", 0) or 0)
    levels_completed = int(sc.get("total_levels_completed", 0) or 0)
    card_id = sc.get("card_id", "?")
    ok = bool(r.get("ok"))

    record = {
        "ts": int(time.time()),
        "ok": ok,
        "env_id": env_id,
        "action_name": "SMART_LS20",
        "steps": steps,
        "score": score,
        "total_actions": total_actions,
        "total_levels_completed": levels_completed,
        "card_id": card_id,
        "duration_sec": round(dt, 1),
    }

    if ok:
        append_history(record)
        update_policy(env_id, "SMART_LS20", score, steps, ok=score > 0)

    return record


def main():
    n_games = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    steps   = int(sys.argv[2]) if len(sys.argv) > 2 else 400
    env_id  = "ls20"

    print(f"=== ARGOS Training: {n_games} забегов на {env_id}, по {steps} шагов ===\n")
    print(f"venv: {arc_play.ARC_VENV_DIR}")
    print(f"history: {HISTORY}")
    print(f"policy: {POLICY}\n")

    scores = []
    levels = []
    for i in range(1, n_games + 1):
        print(f"[{i}/{n_games}] Игра {env_id}...", end=" ", flush=True)
        rec = play_one(env_id, steps)
        if rec["ok"]:
            print(f"score={rec['score']:.2f}% L={rec['total_levels_completed']}/7 "
                  f"actions={rec['total_actions']} card={rec['card_id'][:8]}... ({rec['duration_sec']}s)")
            scores.append(rec["score"])
            levels.append(rec["total_levels_completed"])
        else:
            print(f"FAILED")

    print(f"\n=== Итог ===")
    print(f"Запусков: {len(scores)}")
    if scores:
        print(f"Средний score: {sum(scores)/len(scores):.2f}%")
        print(f"Лучший score: {max(scores):.2f}%")
        print(f"Уровней пройдено: {sum(levels)} (макс {max(levels) if levels else 0}/7 за забег)")
    print(f"\nИстория обновлена: {HISTORY}")


if __name__ == "__main__":
    main()
