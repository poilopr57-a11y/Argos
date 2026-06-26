from __future__ import annotations

import json
import os
import platform
import subprocess
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter(prefix="/argos", tags=["argos"])

MINIAPP_VERSION = "1.0.0"
_STARTED_AT = time.time()
_SERVER_IP = os.getenv("ARGOS_VPN_SERVER_IP", "34.6.44.38")
_MCP_TARGET = os.getenv("ARGOS_MCP_TARGET", "http://127.0.0.1:8000/mcp")


def _psutil_info() -> dict[str, Any]:
    try:
        import psutil
        return {
            "cpu_pct": psutil.cpu_percent(interval=0.3),
            "ram_pct": psutil.virtual_memory().percent,
            "ram_total_gb": round(psutil.virtual_memory().total / (1024**3), 1),
            "ram_free_gb": round(psutil.virtual_memory().available / (1024**3), 1),
            "disk_pct": psutil.disk_usage("/").percent,
            "uptime_seconds": int(time.time() - _STARTED_AT),
        }
    except Exception:
        return {}


async def _handle_command(text: str) -> str:
    t = text.strip().lower()

    if t in ("+", "++", "+ ping", "ping", "пинг", "test", "тест", "эй", "э", "на связи"):
        return "Argos Mini-App работает"

    if t in ("help", "помощь", "команды", "?"):
        return (
            "Доступные команды:\n"
            "+ ping — проверка\n"
            "статус — система\n"
            "gpu — GPU инфо\n"
            "vpn — VPN статус\n"
            "навыки / skills\n"
            "провайдеры / providers\n"
            "версия / version\n"
            "Остальное: @Argosssbot"
        )

    if t in ("status", "статус", "статус системы", "system"):
        info = _psutil_info()
        if info:
            uptime_m = info["uptime_seconds"] // 60
            return (
                f"CPU: {info['cpu_pct']}%\n"
                f"RAM: {info['ram_pct']}% ({info['ram_free_gb']}/{info['ram_total_gb']} GB)\n"
                f"Disk: {info['disk_pct']}%\n"
                f"Uptime: {uptime_m} min\n"
                f"GCP: europe-west4-a"
            )
        return "Status unavailable"

    if t in ("version", "версия"):
        return (
            f"ARGOS Mini-App v{MINIAPP_VERSION}\n"
            f"GCP: argos-vpn-eu\n"
            f"IP: {_SERVER_IP}\n"
            f"Python: {platform.python_version()}"
        )

    if t in ("gpu", "gpu status", "gpu статус"):
        wg = ""
        try:
            r = subprocess.run(["wg", "show"], capture_output=True, text=True, timeout=5)
            wg = r.stdout or r.stderr or ""
        except Exception:
            pass
        return (
            f"WireGuard: {'active' if wg else 'inactive'}\n"
            f"Server key: KJPpkpgajLD/...\n"
            f"Port: 51820/UDP"
        )

    if t in ("vpn", "vpn status", "vpn статус"):
        return (
            f"VPN Server: {_SERVER_IP}:51820\n"
            f"WebApp: vpn.argosssss.win\n"
            f"Status: Active"
        )

    if t in ("skills", "навыки", "список навыков"):
        return (
            "Навыки ARGOS:\n"
            "AI Chat, GPU, P2P, MemPalace,\n"
            "Obsidian, IoT, VPN, Telegram\n"
            "Полный: @Argosssbot /skills"
        )

    if t in ("providers", "провайдеры", "ai"):
        return (
            "AI: Claude, DeepSeek, Kimi,\n"
            "OpenAI, Gemini, Ollama\n"
            "Для AI: @Argosssbot"
        )

    # Try MCP proxy fallback to real ARGOS
    try:
        import aiohttp
        body = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "command", "arguments": {"text": text}},
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                _MCP_TARGET, json=body,
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                data = await resp.json()
                reply = data.get("result", {}).get("content", [{}])[0].get("text", "")
                if reply:
                    return reply
    except Exception:
        pass

    return (
        f"Неизвестно: {text[:50]}\n"
        "Используй @Argosssbot для AI\n"
        "'помощь' для команд"
    )


def _webapp_html() -> str:
    return """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>Argos</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:15px;
  background:var(--tg-theme-bg-color,#0b0f19);color:var(--tg-theme-text-color,#e8ecf4);padding:0;min-height:100vh;overflow-x:hidden}
.wrap{max-width:480px;margin:0 auto;padding:16px 16px 80px}
.card{background:var(--tg-theme-secondary-bg-color,#1a1f2e);border-radius:12px;padding:16px;margin:12px 0}
.card-title{font-size:15px;font-weight:600;margin-bottom:12px;display:flex;align-items:center;gap:8px}
.btn{display:block;width:100%;padding:14px;margin:8px 0;border:none;border-radius:10px;
  background:var(--tg-theme-button-color,#2563eb);color:var(--tg-theme-button-text-color,#fff);
  font-size:16px;font-weight:500;cursor:pointer;transition:opacity .15s}
.btn:active{opacity:.7}.btn:disabled{opacity:.4;cursor:not-allowed}
.btn-sm{padding:10px;font-size:13px;width:auto;display:inline-block;margin:4px}
.btn-outline{background:transparent;border:1px solid var(--tg-theme-button-color,#2563eb);color:var(--tg-theme-button-color,#2563eb)}
.row{display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-bottom:1px solid rgba(255,255,255,.06)}
.row:last-child{border-bottom:none}
.label{color:var(--tg-theme-hint-color,#8e94a2);font-size:13px}
.value{font-size:14px;font-weight:500;text-align:right}
.badge{display:inline-block;padding:2px 10px;border-radius:20px;font-size:12px;font-weight:600}
.badge-ok{background:#16a34a33;color:#16a34a}
.badge-warn{background:#d9770633;color:#d97706}
.badge-off{background:#6b728033;color:#6b7280}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:12px 0}
.grid-tile{background:var(--tg-theme-secondary-bg-color,#1a1f2e);border-radius:10px;padding:14px;text-align:center;cursor:pointer;transition:.15s}
.grid-tile:active{opacity:.6}
.grid-tile .icon{font-size:24px;margin-bottom:4px}
.grid-tile .name{font-size:12px;color:var(--tg-theme-hint-color,#8e94a2)}
.tabs{display:flex;position:fixed;bottom:0;left:0;right:0;background:var(--tg-theme-bg-color,#0b0f19);border-top:1px solid rgba(255,255,255,.06);z-index:100;max-width:480px;margin:0 auto}
.tab{flex:1;padding:12px 4px;text-align:center;font-size:11px;color:var(--tg-theme-hint-color,#8e94a2);cursor:pointer;transition:.15s;border:none;background:none}
.tab.active{color:var(--tg-theme-button-color,#2563eb);font-weight:600}
.tab .tab-icon{font-size:20px;display:block;margin-bottom:2px}
.chat-box{height:50vh;overflow-y:auto;padding:12px;border-radius:10px;background:var(--tg-theme-secondary-bg-color,#1a1f2e);margin:12px 0;display:flex;flex-direction:column;gap:8px}
.chat-msg{padding:10px 14px;border-radius:12px;max-width:85%;word-break:break-word;font-size:14px;line-height:1.4}
.chat-user{background:var(--tg-theme-button-color,#2563eb);color:#fff;align-self:flex-end;border-bottom-right-radius:4px}
.chat-bot{background:#2a3040;color:var(--tg-theme-text-color,#e8ecf4);align-self:flex-start;border-bottom-left-radius:4px}
.chat-input-row{display:flex;gap:8px;align-items:center}
.chat-input{flex:1;padding:12px 16px;border-radius:24px;border:1px solid rgba(255,255,255,.1);background:var(--tg-theme-secondary-bg-color,#1a1f2e);color:var(--tg-theme-text-color,#e8ecf4);font-size:14px;outline:none}
.chat-input:focus{border-color:var(--tg-theme-button-color,#2563eb)}
.loader{text-align:center;padding:20px;color:var(--tg-theme-hint-color,#8e94a2)}
.empty-state{text-align:center;padding:30px;color:var(--tg-theme-hint-color,#8e94a2);font-size:14px}
</style>
</head>
<body>
<div class="wrap" id="app" style="display:none">
  <div id="tab-chat" class="tab-content">
    <div class="card-title"><span>Chat</span></div>
    <div class="chat-box" id="chatBox"></div>
    <div class="chat-input-row">
      <input class="chat-input" id="chatInput" placeholder="Command..." />
      <button class="btn btn-sm" id="chatSend" style="width:auto;padding:12px 20px;border-radius:24px">Go</button>
    </div>
  </div>
  <div id="tab-status" class="tab-content" style="display:none">
    <div class="card-title"><span>System</span></div>
    <div class="card"><div id="systemStats">Loading...</div></div>
    <div class="card-title" style="margin-top:16px"><span>VPN</span></div>
    <div class="card"><div id="vpnStats">Loading...</div></div>
  </div>
  <div id="tab-skills" class="tab-content" style="display:none">
    <div class="card-title"><span>Actions</span></div>
    <div class="grid" id="actionsGrid"></div>
  </div>
  <div id="tab-more" class="tab-content" style="display:none">
    <div class="card-title"><span>About</span></div>
    <div class="card" id="aboutSection"></div>
  </div>
</div>
<div class="tabs" id="tabs">
  <button class="tab active" data-tab="chat"><span class="tab-icon">Chat</span>Chat</button>
  <button class="tab" data-tab="status"><span class="tab-icon">Status</span>Status</button>
  <button class="tab" data-tab="skills"><span class="tab-icon">Skills</span>Skills</button>
  <button class="tab" data-tab="more"><span class="tab-icon">More</span>More</button>
</div>
<div class="loader" id="loader">Loading...</div>
<script>
const tg = window.Telegram.WebApp;
tg.expand();

const $ = function(id) { return document.getElementById(id); };
const chatBox = $('chatBox'), chatInput = $('chatInput'), chatSend = $('chatSend');

async function apiFetch(path, opts) {
  try {
    const r = await fetch(path, opts || {});
    return await r.json();
  } catch(e) {
    return {error: e.message};
  }
}

async function sendCommand(text) {
  const r = await fetch('/argos/api', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({method:'command', params:{text: text}})
  });
  return await r.json();
}

function addMsg(text, isUser) {
  const d = document.createElement('div');
  d.className = 'chat-msg ' + (isUser ? 'chat-user' : 'chat-bot');
  d.textContent = text;
  chatBox.appendChild(d);
  chatBox.scrollTop = chatBox.scrollHeight;
}

chatSend.onclick = async function() {
  const text = chatInput.value.trim();
  if (!text) return;
  addMsg(text, true);
  chatInput.value = '';
  chatSend.disabled = true;
  chatSend.textContent = '...';
  try {
    const data = await sendCommand(text);
    const reply = data.result || data.response || 'No response';
    addMsg(reply, false);
  } catch(e) {
    addMsg('Error: ' + e.message, false);
  }
  chatSend.disabled = false;
  chatSend.textContent = 'Go';
};

chatInput.onkeydown = function(e) {
  if (e.key === 'Enter') chatSend.click();
};

// Tabs
document.querySelectorAll('.tab').forEach(function(tab) {
  tab.onclick = function() {
    document.querySelectorAll('.tab').forEach(function(t) { t.classList.remove('active'); });
    document.querySelectorAll('.tab-content').forEach(function(t) { t.style.display = 'none'; });
    tab.classList.add('active');
    var content = $('tab-' + tab.dataset.tab);
    if (content) content.style.display = 'block';
    if (tab.dataset.tab === 'status') loadStatus();
    if (tab.dataset.tab === 'skills') loadActions();
    if (tab.dataset.tab === 'more') loadAbout();
  };
});

function loadStatus() {
  $('systemStats').innerHTML = 'Loading...';
  sendCommand('status').then(function(data) {
    $('systemStats').innerHTML = '<pre style="font-size:12px;white-space:pre-wrap">' + (data.result || data.response || data.error || 'No data') + '</pre>';
  });
  $('vpnStats').innerHTML = 'Loading...';
  sendCommand('vpn').then(function(data) {
    $('vpnStats').innerHTML = '<pre style="font-size:12px;white-space:pre-wrap">' + (data.result || data.response || data.error || 'No data') + '</pre>';
  });
}

function loadActions() {
  var actions = [
    {name:'System', icon:'S', cmd:'status'},
    {name:'GPU', icon:'G', cmd:'gpu'},
    {name:'VPN', icon:'V', cmd:'vpn'},
    {name:'Skills', icon:'K', cmd:'skills'},
    {name:'AI', icon:'A', cmd:'providers'},
    {name:'Help', icon:'?', cmd:'help'},
  ];
  var html = '';
  actions.forEach(function(a) {
    html += '<div class="grid-tile" onclick="quickCmd(\'' + a.cmd + '\')"><div class="icon">' + a.icon + '</div><div class="name">' + a.name + '</div></div>';
  });
  $('actionsGrid').innerHTML = html;
}

function loadAbout() {
  sendCommand('version').then(function(data) {
    $('aboutSection').innerHTML = '<pre style="font-size:12px;white-space:pre-wrap">' + (data.result || data.response || 'No data') + '</pre>';
  });
}

function quickCmd(cmd) {
  document.querySelectorAll('.tab').forEach(function(t) { t.classList.remove('active'); });
  document.querySelectorAll('.tab-content').forEach(function(t) { t.style.display = 'none'; });
  document.querySelector('[data-tab="chat"]').classList.add('active');
  var chat = $('tab-chat');
  if (chat) chat.style.display = 'block';
  chatInput.value = cmd;
  chatSend.click();
}

// Init
(async function() {
  try {
    var data = await sendCommand('+ ping');
    if (data.result || data.response) {
      $('loader').style.display = 'none';
      $('app').style.display = 'block';
      addMsg('Argos ready. Type help for commands.', false);
    } else {
      $('loader').innerHTML = 'Connection error';
    }
  } catch(e) {
    $('loader').innerHTML = 'Error: ' + e.message;
  }
})();
</script>
</body>
</html>"""


@router.get("/webapp", response_class=HTMLResponse)
async def argos_webapp():
    return _webapp_html()


@router.get("/api")
@router.post("/api")
async def argos_api(request: Request):
    try:
        body = await request.json()
        method = body.get("method", "")
        params = body.get("params", {})
    except Exception:
        return JSONResponse(content={"error": "Invalid request"})

    if method == "command":
        text = params.get("text", "")
        result = await _handle_command(text)
        return JSONResponse(content={"result": result})
    elif method == "health":
        info = _psutil_info()
        return JSONResponse(content={"status": "ok", "version": MINIAPP_VERSION, **info})
    else:
        return JSONResponse(content={"error": "Unknown method"})


@router.get("/health")
async def argos_health():
    return {"status": "ok", "version": MINIAPP_VERSION, "server": _SERVER_IP}
