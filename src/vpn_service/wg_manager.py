"""WireGuard runtime manager for Argos VPN bot."""

from __future__ import annotations

import logging
import os
import re
import subprocess
from typing import Optional

logger = logging.getLogger("argos.vpn.wg_manager")


class WireGuardManager:
    """Wraps wg/wg-quick CLI with input validation and safe fallback."""

    def __init__(self, interface: str = "wg0"):
        self.interface = interface
        self.server_private_key = os.getenv("ARGOS_WG_SERVER_PRIVATE_KEY", os.getenv("WG_PRIVATE_KEY", ""))
        self.server_public_key = self._derive_public()
        self.dry_run = os.getenv("ARGOS_VPN_DRY_RUN", "false").lower() in ("1", "true", "yes")

    def _derive_public(self) -> str:
        if not self.server_private_key:
            return ""
        try:
            proc = subprocess.run(
                ["wg", "pubkey"],
                input=self.server_private_key,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if proc.returncode != 0:
                logger.warning("wg pubkey failed: %s", proc.stderr.strip())
                return ""
            return proc.stdout.strip()
        except FileNotFoundError:
            logger.warning("wg binary not found — running in dry-run mode")
            return ""
        except Exception as exc:
            logger.warning("wg pubkey error: %s", exc)
            return ""

    def _safe_key(self, key: str) -> str:
        """Strip and validate base64-ish WireGuard key."""
        if not isinstance(key, str):
            raise ValueError("key must be string")
        key = key.strip()
        if not key:
            raise ValueError("empty key")
        if not re.match(r"^[A-Za-z0-9+/=]{40,48}$", key):
            raise ValueError("invalid key format")
        return key

    def generate_keypair(self) -> dict[str, str]:
        if self.dry_run:
            import base64
            import secrets

            raw_priv = secrets.token_bytes(32)
            raw_pub = secrets.token_bytes(32)
            priv = base64.b64encode(raw_priv).decode()
            pub = base64.b64encode(raw_pub).decode()
            return {"private_key": priv, "public_key": pub}
        try:
            priv = subprocess.check_output(["wg", "genkey"], text=True, timeout=5).strip()
            proc = subprocess.run(
                ["wg", "pubkey"],
                input=priv,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if proc.returncode != 0:
                raise RuntimeError(f"wg pubkey failed: {proc.stderr.strip()}")
            return {"private_key": priv, "public_key": proc.stdout.strip()}
        except FileNotFoundError as exc:
            raise RuntimeError("wg binary not found") from exc

    def add_peer(self, public_key: str, ip_address: str) -> None:
        public_key = self._safe_key(public_key)
        if not ip_address or not re.match(r"^\d+\.\d+\.\d+\.\d+$", ip_address):
            raise ValueError("invalid ip_address")
        if self.dry_run:
            logger.info("dry-run: wg set %s peer %s allowed-ips %s/32", self.interface, public_key, ip_address)
            return
        subprocess.run(
            [
                "wg", "set", self.interface,
                "peer", public_key,
                "allowed-ips", f"{ip_address}/32",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def remove_peer(self, public_key: str) -> None:
        public_key = self._safe_key(public_key)
        if self.dry_run:
            logger.info("dry-run: wg set %s peer %s remove", self.interface, public_key)
            return
        subprocess.run(
            ["wg", "set", self.interface, "peer", public_key, "remove"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def generate_client_config(
        self,
        private_key: str,
        ip_address: str,
        server_ip: Optional[str] = None,
        server_port: Optional[int] = None,
    ) -> str:
        private_key = self._safe_key(private_key)
        if not ip_address or not re.match(r"^\d+\.\d+\.\d+\.\d+$", ip_address):
            raise ValueError("invalid ip_address")
        server_ip = server_ip or os.getenv("ARGOS_VPN_SERVER_IP", os.getenv("SERVER_IP", "your-server.com"))
        server_port = server_port or int(os.getenv("ARGOS_VPN_PORT", os.getenv("SERVER_PORT", "51820")))
        server_pubkey = self.server_public_key or os.getenv("ARGOS_WG_SERVER_PUBLIC_KEY", "")
        return (
            "[Interface]\n"
            f"PrivateKey = {private_key}\n"
            f"Address = {ip_address}/32\n"
            "DNS = 1.1.1.1\n\n"
            "[Peer]\n"
            f"PublicKey = {server_pubkey}\n"
            f"Endpoint = {server_ip}:{server_port}\n"
            "AllowedIPs = 0.0.0.0/0, ::/0\n"
            "PersistentKeepalive = 25\n"
        )

    def show_dump(self) -> list[dict[str, int | str]]:
        """Return current wg show dump as list of peer dicts."""
        if self.dry_run:
            return []
        try:
            dump = subprocess.check_output(
                ["wg", "show", self.interface, "dump"],
                text=True,
                timeout=10,
            )
        except FileNotFoundError:
            logger.warning("wg binary not found")
            return []
        except subprocess.CalledProcessError:
            return []

        peers: list[dict[str, int | str]] = []
        for line in dump.strip().split("\n"):
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            try:
                rx = int(parts[5])
                tx = int(parts[6])
            except ValueError:
                continue
            peers.append({
                "public_key": parts[0].strip(),
                "rx": rx,
                "tx": tx,
            })
        return peers
