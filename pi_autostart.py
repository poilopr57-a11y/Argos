#!/usr/bin/env python3
"""
pi_autostart.py — Статус и тест Pi Coding Agent

Pi — это CLI-агент (как Claude Code), НЕ сервер.
Не требует запуска "в фоне" — вызывается по запросу через pi_bridge.

Использование:
  python pi_autostart.py              # проверка установки
  python pi_autostart.py --status     # статус Pi
  python pi_autostart.py --test       # тестовый запрос
"""

import os
import sys
import shutil
import subprocess
import time
from pathlib import Path

ARGOS_DIR = Path(__file__).parent.resolve()
PI_CMD    = os.getenv("PI_CMD", "pi")
KIMI_KEY  = os.getenv("KIMI_API_KEY", "")


def log(msg):
    ts = time.strftime("%H:%M:%S")
    print(f"[Pi {ts}] {msg}")


def find_pi() -> str:
    """Найти путь к Pi CLI."""
    if shutil.which(PI_CMD):
        return PI_CMD
    win_path = r"C:\Users\AvA\AppData\Roaming\npm\pi.cmd"
    if os.path.exists(win_path):
        return win_path
    return PI_CMD


def get_version(pi_path: str) -> str:
    try:
        r = subprocess.run([pi_path, "--version"], capture_output=True, text=True,
                           shell=pi_path.endswith(".cmd"), timeout=8)
        return (r.stdout.strip() or r.stderr.strip() or "unknown").splitlines()[0]
    except Exception as e:
        return f"error: {e}"


def run_task(task: str, pi_path: str, timeout: int = 120) -> str:
    """Выполнить задачу через Pi (non-interactive)."""
    cmd = [pi_path, "--print", "--no-session"]
    if KIMI_KEY:
        cmd += ["--provider", "kimi", "--api-key", KIMI_KEY]
    cmd.append(task)
    try:
        r = subprocess.run(
            " ".join(f'"{a}"' if " " in a else a for a in cmd),
            shell=True, capture_output=True, text=True,
            timeout=timeout, cwd=str(ARGOS_DIR)
        )
        return (r.stdout or r.stderr or "[пустой ответ]").strip()
    except subprocess.TimeoutExpired:
        return f"[Pi] Timeout {timeout}s"
    except Exception as e:
        return f"[Pi] Error: {e}"


def status():
    pi_path = find_pi()
    ver = get_version(pi_path)
    provider = "kimi (KIMI_API_KEY)" if KIMI_KEY else "default"
    print(f"Pi CLI: {'✅ ' + ver if 'error' not in ver else '❌ ' + ver}")
    print(f"Path:   {pi_path}")
    print(f"Provider: {provider}")
    print(f"CWD:    {ARGOS_DIR}")
    print()
    print("Pi — CLI-агент. Запуск: pi_bridge.execute('задача') из ARGOS.")
    print("Нет постоянного сервера — вызывается по требованию.")


def test():
    pi_path = find_pi()
    ver = get_version(pi_path)
    if "error" in ver.lower():
        print(f"❌ Pi не найден: {ver}")
        return

    print(f"✅ Pi {ver} найден. Выполняю тестовый запрос...")
    result = run_task("Напиши однострочный Python-принт 'ARGOS Pi OK'", pi_path, timeout=60)
    print(f"Ответ Pi:\n{result[:500]}")


def main():
    if "--test" in sys.argv:
        test()
    elif "--status" in sys.argv or not sys.argv[1:]:
        status()
    else:
        print("Использование: python pi_autostart.py [--status|--test]")


if __name__ == "__main__":
    main()
