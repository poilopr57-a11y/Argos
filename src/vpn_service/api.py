"""FastAPI routes for Argos VPN client management."""

from __future__ import annotations

import os
import time
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from src.vpn_service.database import Database
from src.vpn_service.wg_manager import WireGuardManager


class RegisterRequest(BaseModel):
    telegram_id: int = Field(..., gt=0)
    username: Optional[str] = Field(None, max_length=64)


class ClientConfigRequest(BaseModel):
    telegram_id: int = Field(..., gt=0)


class AdminCleanupResponse(BaseModel):
    deactivated: int
    public_keys: list[str]


router = APIRouter(prefix="/vpn", tags=["vpn"])


def _webapp_html() -> str:
    url = os.getenv("ARGOS_VPN_WEBAPP_URL", "https://example.com/vpn/webapp")
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Argos VPN</title>
<style>
  body {{ font-family: system-ui, sans-serif; background:#0b0f19; color:#e8ecf4; margin:0; padding:20px; }}
  .wrap {{ max-width:480px; margin:0 auto; }}
  h1 {{ text-align:center; }}
  .btn {{ display:block; width:100%; padding:14px; margin:12px 0; border:none; border-radius:10px;
         background:#2563eb; color:#fff; font-size:16px; cursor:pointer; }}
  .status {{ background:#111827; padding:14px; border-radius:10px; margin-top:16px; white-space:pre-wrap; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>Argos VPN</h1>
  <button class="btn" id="getConfig">Получить конфиг</button>
  <button class="btn" id="getStatus">Статус</button>
  <div class="status" id="status">Ожидание действия...</div>
</div>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<script>
  const tg = window.Telegram.WebApp;
  tg.ready();
  const user = tg.initDataUnsafe.user || {{}};
  const status = document.getElementById('status');
  async function api(action) {{
    const body = {{ telegram_id: user.id, username: user.username || '' }};
    const r = await fetch('/vpn/client/' + (action === 'status' ? user.id : 'create'), {{
      method: action === 'status' ? 'GET' : 'POST',
      headers: {{'Content-Type':'application/json'}},
      body: action === 'status' ? undefined : JSON.stringify(body)
    }});
    const data = await r.json().catch(() => ({{error: 'network'}}));
    status.textContent = JSON.stringify(data, null, 2);
  }}
  document.getElementById('getConfig').onclick = () => api('create');
  document.getElementById('getStatus').onclick = () => api('status');
</script>
</body>
</html>"""


@router.get("/webapp", response_class=HTMLResponse)
async def vpn_webapp() -> str:
    return _webapp_html()


def _get_db() -> Database:
    return Database(db_path=os.getenv("ARGOS_VPN_DB_PATH"))


def _get_wg() -> WireGuardManager:
    return WireGuardManager(interface=os.getenv("ARGOS_VPN_INTERFACE", "wg0"))


@router.get("/health")
async def vpn_health() -> dict[str, Any]:
    return {"status": "ok", "service": "argos-vpn"}


@router.post("/register")
async def register_user(request: RegisterRequest) -> dict[str, Any]:
    db = _get_db()
    user = db.get_user(request.telegram_id)
    if not user:
        user = db.create_user(request.telegram_id, request.username)
    return {"status": "ok", "telegram_id": request.telegram_id, "username": user.get("username")}


@router.post("/client/create")
async def create_client(request: ClientConfigRequest) -> dict[str, Any]:
    db = _get_db()
    wg = _get_wg()

    user = db.get_user(request.telegram_id)
    if not user:
        user = db.create_user(request.telegram_id)

    existing = db.get_active_key(user["id"])
    if existing:
        server_ip = os.getenv("ARGOS_VPN_SERVER_IP", os.getenv("SERVER_IP", "your-server.com"))
        config = wg.generate_client_config(
            existing["private_key"], existing["ip_address"], server_ip=server_ip
        )
        return {
            "status": "existing",
            "telegram_id": request.telegram_id,
            "ip": existing["ip_address"],
            "public_key": existing["public_key"],
            "expires_at": existing["expires_at"],
            "config": config,
        }

    try:
        ip = db.allocate_ip()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    try:
        kp = wg.generate_keypair()
    except RuntimeError as exc:
        db.release_ip(ip)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    db.create_key(user["id"], kp["private_key"], kp["public_key"], ip, ttl_days=3)
    try:
        wg.add_peer(kp["public_key"], ip)
    except Exception as exc:
        db.deactivate_key(kp["public_key"])
        raise HTTPException(status_code=500, detail=f"WireGuard error: {exc}") from exc

    server_ip = os.getenv("ARGOS_VPN_SERVER_IP", os.getenv("SERVER_IP", "your-server.com"))
    config = wg.generate_client_config(kp["private_key"], ip, server_ip=server_ip)
    return {
        "status": "created",
        "telegram_id": request.telegram_id,
        "ip": ip,
        "public_key": kp["public_key"],
        "expires_at": int(time.time()) + 3 * 86400,
        "config": config,
    }


@router.get("/client/{telegram_id}")
async def get_client(telegram_id: int) -> dict[str, Any]:
    db = _get_db()
    user = db.get_user(telegram_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    key = db.get_active_key(user["id"])
    if not key:
        raise HTTPException(status_code=404, detail="No active config")
    traffic_bytes = db.get_traffic(telegram_id)
    return {
        "telegram_id": telegram_id,
        "ip": key["ip_address"],
        "public_key": key["public_key"],
        "expires_at": key["expires_at"],
        "days_left": max(0, (key["expires_at"] - int(time.time())) // 86400),
        "traffic_gb": round(traffic_bytes / (1024**3), 2),
    }


@router.post("/admin/cleanup")
async def admin_cleanup() -> AdminCleanupResponse:
    db = _get_db()
    wg = _get_wg()
    released = db.cleanup_expired_keys()
    for pubkey in released:
        try:
            wg.remove_peer(pubkey)
        except Exception:
            pass
    return AdminCleanupResponse(deactivated=len(released), public_keys=released)


@router.get("/clients")
async def list_clients() -> dict[str, Any]:
    db = _get_db()
    keys = db.list_active_keys()
    return {"count": len(keys), "clients": keys}
