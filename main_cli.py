# ARGOS CLI Android entry point
# -*- coding: utf-8 -*-
"""
main_cli.py — CLI-only entry point for Android APK (no Kivy).
"""

import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("ARGOS_SKIP_GUI", "1")
os.environ.setdefault("ARGOS_NO_GPU", "1")
os.environ.setdefault("ARGOS_MODE", "cli")


def main():
    print("ARGOS CLI v2.1.3 — Android")
    try:
        from src.core import ArgosCore
        core = ArgosCore()
        status = core.status()
        print(f"ARGOS core: {status}")
        print("Type commands (or 'exit'):")
        while True:
            try:
                cmd = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if cmd.lower() in ("exit", "quit", "q"):
                break
            if not cmd:
                continue
            try:
                resp = core.process_logic(cmd)
                print(resp or "(no response)")
            except Exception as e:
                print(f"ERR: {e}")
    except ImportError as e:
        print(f"ARGOS init failed: {e}")
        print("Fallback: telnet-like loop on port 8000")
        try:
            import socket
            s = socket.socket()
            s.connect(("127.0.0.1", 8000))
            s.send(b"GET /health HTTP/1.0\r\n\r\n")
            print(s.recv(1024).decode())
            s.close()
        except Exception as e2:
            print(f"Fallback also failed: {e2}")


if __name__ == "__main__":
    main()
