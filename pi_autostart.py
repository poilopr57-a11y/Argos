#!/usr/bin/env python3
"""
pi_autostart.py — Автозапуск Pi Coding Agent при старте ARGOS

Использование:
  python pi_autostart.py              # запуск в фоне
  python pi_autostart.py --foreground # видимый режим
  python pi_autostart.py --status     # статус Pi
"""

import os
import sys
import time
import subprocess
import requests
import threading
from pathlib import Path

# Конфигурация
ARGOS_DIR = Path(__file__).parent.resolve()
PI_BASE_URL = os.getenv("PI_BASE_URL", "http://localhost:18765")
PI_CMD = os.getenv("PI_CMD", "pi")
HEALTH_CHECK_INTERVAL = 30  # секунд
SESSIONS_DIR = ARGOS_DIR / ".pi" / "sessions"


def log(msg):
    """Логирование с меткой времени."""
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[Pi-AutoStart {ts}] {msg}")


def find_pi_path():
    """Найти путь к Pi."""
    import shutil
    # Сначала в PATH
    if shutil.which("pi"):
        return "pi"
    # Windows npm путь
    win_path = r"C:\Users\AvA\AppData\Roaming\npm\pi.cmd"
    if os.path.exists(win_path):
        return win_path
    return "pi"  # fallback


def is_pi_running():
    """Проверить, запущен ли Pi."""
    try:
        r = requests.get(f"{PI_BASE_URL}/health", timeout=3)
        return r.ok
    except:
        return False


def save_session_memory(pid, started_at):
    """Сохранить информацию о сессии Pi."""
    mem_path = ARGOS_DIR / "AGENTS.md"
    session_info = f"""
## Pi Session — {time.strftime("%Y-%m-%d %H:%M:%S")}
- PID: {pid}
- Started: {started_at}
- CWD: {ARGOS_DIR}
- URL: {PI_BASE_URL}
"""
    try:
        with open(mem_path, "a", encoding="utf-8") as f:
            f.write(session_info)
        log(f"Память сохранена: {mem_path}")
    except Exception as e:
        log(f"Ошибка сохранения памяти: {e}")


def start_pi_background():
    """Запустить Pi в фоновом режиме."""
    pi_path = find_pi_path()
    log(f"Запуск Pi: {pi_path}")
    
    try:
        proc = subprocess.Popen(
            [pi_path, "server"],
            cwd=str(ARGOS_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "PI_CWD": str(ARGOS_DIR)},
        )
        started = time.strftime("%Y-%m-%d %H:%M:%S")
        log(f"Pi запущен (PID {proc.pid}) в {started}")
        
        # Сохраняем память
        save_session_memory(proc.pid, started)
        
        # Ждём запуска
        for i in range(10):
            time.sleep(2)
            if is_pi_running():
                log(f"Pi готов к работе: {PI_BASE_URL}")
                return True
            log(f"Ожидание запуска Pi... ({i+1}/10)")
        
        log("Pi не ответил за 20 секунд")
        return False
        
    except Exception as e:
        log(f"Ошибка запуска Pi: {e}")
        return False


def watchdog_loop():
    """Следить за Pi и перезапускать при падении."""
    log("Watchdog запущен")
    while True:
        time.sleep(HEALTH_CHECK_INTERVAL)
        if not is_pi_running():
            log("Pi не отвечает — перезапуск...")
            start_pi_background()


def main():
    if "--status" in sys.argv:
        if is_pi_running():
            print(f"✅ Pi работает: {PI_BASE_URL}")
        else:
            print(f"❌ Pi не запущен")
        return
    
    if "--foreground" in sys.argv:
        # Запуск в видимом режиме
        pi_path = find_pi_path()
        subprocess.run([pi_path, "server"], cwd=str(ARGOS_DIR))
    else:
        # Фоновый режим
        if is_pi_running():
            log("Pi уже запущен")
        else:
            start_pi_background()
        
        # Запускаем watchdog
        watchdog_thread = threading.Thread(target=watchdog_loop, daemon=True, name="PiWatchdog")
        watchdog_thread.start()
        
        log("Pi AutoStart работает в фоне. Нажми Ctrl+C для остановки.")
        try:
            while True:
                time.sleep(10)
        except KeyboardInterrupt:
            log("Остановка...")
            sys.exit(0)


if __name__ == "__main__":
    main()