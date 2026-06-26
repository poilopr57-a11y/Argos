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
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         background: var(--tg-theme-bg-color, #0b0f19);
         color: var(--tg-theme-text-color, #e8ecf4);
         padding:16px; min-height:100vh; }}
  .wrap {{ max-width:480px; margin:0 auto; }}
  .card {{ background: var(--tg-theme-secondary-bg-color, #1a1f2e);
          border-radius:12px; padding:16px; margin:12px 0; }}
  h1 {{ font-size:22px; text-align:center; margin-bottom:4px; }}
  .sub {{ text-align:center; color: var(--tg-theme-hint-color, #8e94a2); font-size:13px; margin-bottom:16px; }}
  .btn {{ display:block; width:100%; padding:14px; margin:8px 0; border:none; border-radius:10px;
         background: var(--tg-theme-button-color, #2563eb);
         color: var(--tg-theme-button-text-color, #fff);
         font-size:16px; font-weight:500; cursor:pointer; transition:opacity .15s; }}
  .btn:active {{ opacity:.7; }}
  .btn:disabled {{ opacity:.4; cursor:not-allowed; }}
  .btn-outline {{ background:transparent; border:1px solid var(--tg-theme-button-color, #2563eb);
                 color: var(--tg-theme-button-color, #2563eb); }}
  .config-box {{ background: #0d1117; border-radius:8px; padding:12px; font-family: 'SF Mono', monospace;
                font-size:12px; line-height:1.5; overflow-x:auto; white-space:pre-wrap; word-break:break-all;
                max-height:300px; overflow-y:auto; user-select:all; }}
  .row {{ display:flex; justify-content:space-between; padding:8px 0; border-bottom:1px solid rgba(255,255,255,.06); }}
  .row:last-child {{ border-bottom:none; }}
  .label {{ color: var(--tg-theme-hint-color, #8e94a2); font-size:13px; }}
  .value {{ font-size:14px; font-weight:500; text-align:right; }}
  .badge {{ display:inline-block; padding:2px 10px; border-radius:20px; font-size:12px; font-weight:600; }}
  .badge-ok {{ background:#16a34a22; color:#4ade80; }}
  .badge-warn {{ background:#ea580c22; color:#fb923c; }}
  .badge-exp {{ background:#dc262622; color:#f87171; }}
  .toast {{ position:fixed; bottom:24px; left:50%; transform:translateX(-50%);
           background: var(--tg-theme-button-color, #2563eb);
           color: var(--tg-theme-button-text-color, #fff);
           padding:10px 24px; border-radius:10px; font-size:14px; font-weight:500;
           opacity:0; transition:opacity .3s; pointer-events:none; z-index:99; }}
  .toast.show {{ opacity:1; }}
  .loader {{ text-align:center; padding:40px 0; color: var(--tg-theme-hint-color, #8e94a2); }}
</style>
</head>
<body>
<div class="wrap">
  <h1>Argos VPN</h1>
  <p class="sub">Безопасный и анонимный VPN-доступ</p>

  <div id="main" style="display:none;">
    <button class="btn" id="getConfigBtn">Получить VPN-конфиг</button>
    <button class="btn btn-outline" id="getStatusBtn">Статус подключения</button>

    <div id="configCard" class="card" style="display:none;">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
        <strong style="font-size:15px;">Ваш конфиг</strong>
        <span style="font-size:12px; color:var(--tg-theme-hint-color,#8e94a2);">Действует 3 дня</span>
      </div>
      <pre class="config-box" id="configText"></pre>
      <div style="display:flex; gap:8px; margin-top:10px;">
        <button class="btn" id="copyBtn" style="flex:1; padding:10px; font-size:14px;">Копировать</button>
        <button class="btn" id="downloadBtn" style="flex:1; padding:10px; font-size:14px;">Скачать</button>
      </div>
    </div>

    <div id="statusCard" class="card" style="display:none;">
      <div id="statusRows"></div>
    </div>
  </div>

  <div id="loader" class="loader">
    <p>Загрузка...</p>
  </div>
  <div id="errorBlock" class="card" style="display:none; text-align:center;">
    <p style="color:#f87171;font-size:15px;" id="errorText"></p>
    <button class="btn" onclick="location.reload()" style="margin-top:12px;">Обновить</button>
  </div>
</div>

<div class="toast" id="toast"></div>

<script src="https://telegram.org/js/telegram-web-app.js"></script>
<script>
const tg = window.Telegram.WebApp;
tg.ready();
tg.expand();

const user = tg.initDataUnsafe.user || {};
const uid = user.id;
const uname = user.username || '';

function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2000);
}

function showError(msg) {
  document.getElementById('loader').style.display = 'none';
  const eb = document.getElementById('errorBlock');
  document.getElementById('errorText').textContent = msg;
  eb.style.display = 'block';
}

function daysLeft(ts) {
  const d = Math.max(0, Math.floor((ts - Date.now()/1000) / 86400));
  return d;
}

function trafficGB(bytes) {
  return (bytes / (1024**3)).toFixed(2);
}

function formatDate(ts) {
  const d = new Date(ts * 1000);
  return d.toLocaleDateString('ru-RU', {day:'numeric', month:'long', year:'numeric'});
}

async function getConfig() {
  if (!uid) { showError('Не удалось получить данные пользователя Telegram'); return; }
  const btn = document.getElementById('getConfigBtn');
  btn.disabled = true; btn.textContent = 'Создание...';
  try {
    const r = await fetch('/vpn/client/create', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({telegram_id: uid, username: uname})
    });
    const data = await r.json();
    if (data.config) {
      document.getElementById('configText').textContent = data.config;
      document.getElementById('configCard').style.display = 'block';
      showToast(data.status === 'existing' ? 'Конфиг загружен' : 'Новый конфиг создан');
    } else {
      showError(data.detail || 'Ошибка создания конфига');
    }
  } catch(e) {
    showError('Ошибка сети. Проверьте подключение.');
  } finally {
    btn.disabled = false; btn.textContent = 'Получить VPN-конфиг';
  }
}

async function getStatus() {
  if (!uid) { showError('Нет данных пользователя'); return; }
  const card = document.getElementById('statusCard');
  const rows = document.getElementById('statusRows');
  card.style.display = 'none';
  try {
    const r = await fetch('/vpn/client/' + uid);
    if (r.status === 404) {
      rows.innerHTML = '<div class="row"><span class="label">Статус</span><span class="badge badge-warn">Нет конфига</span></div>' +
        '<p style="text-align:center;margin-top:12px;font-size:13px;color:var(--tg-theme-hint-color,#8e94a2);">Нажмите «Получить VPN-конфиг»</p>';
      card.style.display = 'block';
      return;
    }
    const data = await r.json();
    const dl = daysLeft(data.expires_at);
    const badge = dl > 1 ? 'badge-ok' : (dl === 1 ? 'badge-warn' : 'badge-exp');
    rows.innerHTML =
      `<div class="row"><span class="label">Статус</span><span class="badge ${badge}">${dl > 0 ? 'Активен' : 'Истёк'}</span></div>` +
      `<div class="row"><span class="label">Ваш IP</span><span class="value">${data.ip || '—'}</span></div>` +
      `<div class="row"><span class="label">Осталось дней</span><span class="value">${dl}</span></div>` +
      `<div class="row"><span class="label">Истекает</span><span class="value">${formatDate(data.expires_at)}</span></div>` +
      `<div class="row"><span class="label">Трафик</span><span class="value">${trafficGB(data.traffic_gb || 0)} GB / 5 GB</span></div>`;
    card.style.display = 'block';
  } catch(e) {
    showError('Ошибка сети');
  }
}

document.getElementById('getConfigBtn').onclick = getConfig;
document.getElementById('getStatusBtn').onclick = getStatus;

document.getElementById('copyBtn').onclick = function() {
  const text = document.getElementById('configText').textContent;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(() => showToast('Конфиг скопирован'));
  } else {
    const ta = document.createElement('textarea');
    ta.value = text; document.body.appendChild(ta); ta.select();
    document.execCommand('copy'); document.body.removeChild(ta);
    showToast('Конфиг скопирован');
  }
};

document.getElementById('downloadBtn').onclick = function() {
  const text = document.getElementById('configText').textContent;
  const blob = new Blob([text], {type: 'text/plain;charset=utf-8'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'argos_' + (uname || uid) + '.conf';
  a.click();
  URL.revokeObjectURL(a.href);
  showToast('Файл скачан');
};

if (!uid) {
  document.getElementById('loader').style.display = 'none';
  showError('Запустите приложение через Telegram');
} else {
  document.getElementById('loader').style.display = 'none';
  document.getElementById('main').style.display = 'block';
  getStatus();
}
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
