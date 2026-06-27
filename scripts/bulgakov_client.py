"""
Bulgakov Tunnel CLI Client — стеганографический VPN-клиент через Мастер и Маргарита.
Usage:
  python bulgakov_client.py send "Hello ARGOS"
  python bulgakov_client.py proxy --port 1080  # SOCKS5 proxy
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import random
import sys
import time
from pathlib import Path

import requests  # optional: only needed for client


class BulgakovClient:
    """Лёгкий клиент для стеганографического туннеля."""

    def __init__(self, server_url: str, pdf_path: str = "", book_url: str = ""):
        self.server = server_url.rstrip("/")
        self.codec = None
        self._init_codec(pdf_path, book_url)

    def _init_codec(self, pdf_path: str, book_url: str) -> None:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        try:
            from src.vpn_service.bulgakov_tunnel import BulgakovCodec
            self.codec = BulgakovCodec(pdf_path or "")
        except Exception:
            # Fallback: download book and init
            if book_url:
                import tempfile
                tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
                r = requests.get(book_url, timeout=30)
                tmp.write(r.content)
                tmp.close()
                from src.vpn_service.bulgakov_tunnel import BulgakovCodec
                self.codec = BulgakovCodec(tmp.name)
            else:
                raise RuntimeError("No codec available")

    def ping(self) -> dict:
        r = requests.get(f"{self.server}/tunnel/health", timeout=10)
        return r.json()

    def send(self, data: bytes) -> bytes:
        """Отправка через bulk-туннель: сжатие → координаты → POST."""
        if not self.codec:
            raise RuntimeError("Codec not initialized")
        coords = self.codec.encode_bulk(data, compress=True)
        payload = {"coords": coords}
        r = requests.post(
            f"{self.server}/tunnel/bulk",
            json=payload,
            timeout=30,
            headers={"Content-Type": "application/json"},
        )
        if r.status_code != 200:
            raise RuntimeError(f"Tunnel error: {r.status_code}")
        result = r.json()
        if "data" in result:
            return base64.b64decode(result["data"])
        return b""


def cmd_send(args):
    client = BulgakovClient(args.server, pdf_path=args.book, book_url=args.book_url)
    print(f"Connected: {client.ping()}")
    data = args.text.encode() if args.text else sys.stdin.buffer.read()
    start = time.time()
    response = client.send(data)
    elapsed = time.time() - start
    print(f"Sent {len(data)}B -> received {len(response)}B in {elapsed*1000:.0f}ms")
    if args.text:
        print(f"Response: {response.decode('utf-8', errors='replace')[:200]}")


def cmd_ping(args):
    client = BulgakovClient(args.server)
    print(json.dumps(client.ping(), indent=2))


def main():
    parser = argparse.ArgumentParser(description="Bulgakov Stealth Tunnel Client")
    parser.add_argument("--server", default="https://vpn.argosssss.win", help="Tunnel server URL")
    parser.add_argument("--book-url", default="https://www.bulgakov.ru/pdf/Master-i-Margarita.pdf")

    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("ping", help="Check tunnel health")
    p.set_defaults(func=cmd_ping)

    s = sub.add_parser("send", help="Send data through tunnel")
    s.add_argument("text", nargs="?", help="Text to send (or pipe stdin)")
    s.add_argument("--book", default="", help="Path to local PDF")
    s.set_defaults(func=cmd_send)

    args = parser.parse_args()
    if not args.cmd:
        parser.print_help()
        return
    args.func(args)


if __name__ == "__main__":
    main()
