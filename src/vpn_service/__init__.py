"""Argos VPN Service — WireGuard client management for Telegram mini-app."""

from src.vpn_service.database import Database
from src.vpn_service.wg_manager import WireGuardManager

__all__ = ["Database", "WireGuardManager", "api"]
