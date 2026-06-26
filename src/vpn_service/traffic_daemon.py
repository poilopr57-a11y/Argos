"""Background traffic collector for Argos VPN bot."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from src.vpn_service.database import Database
from src.vpn_service.wg_manager import WireGuardManager

logger = logging.getLogger("argos.vpn.traffic_daemon")


async def collect_traffic(
    db: Database,
    wg: WireGuardManager,
    interval_seconds: int = 60,
) -> None:
    """Poll wg show dump and accumulate traffic per user."""
    if not isinstance(interval_seconds, int) or interval_seconds < 10:
        interval_seconds = 60
    while True:
        try:
            peers = wg.show_dump()
            for peer in peers:
                pubkey = str(peer.get("public_key", ""))
                rx = int(peer.get("rx", 0))
                tx = int(peer.get("tx", 0))
                user = db.get_user_by_pubkey(pubkey)
                if user:
                    db.update_traffic(user["telegram_id"], rx + tx)
        except Exception as exc:
            logger.error("traffic_collector error: %s", exc)
        await asyncio.sleep(interval_seconds)


async def run_daemon(
    db_path: Optional[str] = None,
    interface: str = "wg0",
    interval_seconds: int = 60,
) -> None:
    db = Database(db_path=db_path)
    wg = WireGuardManager(interface=interface)
    logger.info("traffic_daemon started interface=%s interval=%s", interface, interval_seconds)
    await collect_traffic(db, wg, interval_seconds)
