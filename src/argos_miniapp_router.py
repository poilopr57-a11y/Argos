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

    # Only keep essential local commands
    if t in ("+", "++", "+ ping", "ping", "пинг", "test", "тест", "эй", "э", "на связи"):
        return "Argos работает"

    if t in ("help", "помощь", "команды", "?"):
        return "Напиши любой вопрос — ARGOS ответит через ИИ. /help для списка команд."

    if t in ("version", "версия"):
        import platform as _plt
        return f"ARGOS Mini-App v{MINIAPP_VERSION}\nGCP: argos-vpn-eu\nIP: {_SERVER_IP}\nPython: {_plt.python_version()}"

    # Everything else goes to real MCP via Cloudflare tunnel
    try:
        import aiohttp
        body = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "command", "arguments": {"text": text}},
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                _MCP_TARGET, json=body,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                data = await resp.json()
                reply = data.get("result", {}).get("content", [{}])[0].get("text", "")
                if reply:
                    return reply
    except Exception as exc:
        return f"MCP недоступен: {exc}"

    return "MCP не ответил. Попробуй ещё раз или напиши @Argosssbot"


def _webapp_html() -> str:
    return """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
<title>Argos</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--tg-theme-bg-color,#0b0f19);color:var(--tg-theme-text-color,#e8ecf4);font-family:sans-serif;font-size:15px;height:100dvh;display:flex;flex-direction:column}
.header{padding:12px 16px;font-weight:600;font-size:16px;border-bottom:1px solid rgba(255,255,255,.06)}
.chat{flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:8px}
.input-row{display:flex;gap:8px;padding:12px;border-top:1px solid rgba(255,255,255,.06);background:var(--tg-theme-bg-color,#0b0f19)}
.input{flex:1;padding:12px 16px;border-radius:24px;border:1px solid rgba(255,255,255,.1);background:var(--tg-theme-secondary-bg-color,#1a1f2e);color:var(--tg-theme-text-color,#e8ecf4);font-size:14px;outline:none}
.input:focus{border-color:var(--tg-theme-button-color,#2563eb)}
.send{width:48px;height:48px;border-radius:50%;border:none;background:var(--tg-theme-button-color,#2563eb);color:#fff;font-size:20px;cursor:pointer;flex-shrink:0}
.send:active{opacity:.7}
.msg{padding:10px 14px;border-radius:12px;max-width:85%;font-size:14px;line-height:1.4}
.u{background:var(--tg-theme-button-color,#2563eb);color:#fff;align-self:flex-end;border-bottom-right-radius:4px}
.b{background:#2a3040;color:var(--tg-theme-text-color,#e8ecf4);align-self:flex-start;border-bottom-left-radius:4px}
</style>
</head>
<body>
<div class="header">Argos</div>
<div class="chat" id="chat"></div>
<div class="input-row">
<input class="input" id="input" placeholder="Command..." />
<button class="send" id="send">→</button>
</div>
<script>
var tg = window.Telegram.WebApp;
if (tg) { tg.ready(); tg.expand(); }
var chat = document.getElementById('chat');
var input = document.getElementById('input');
var send = document.getElementById('send');

function addMsg(t, u) {
  var d = document.createElement('div');
  d.className = 'msg ' + (u ? 'u' : 'b');
  d.textContent = t;
  chat.appendChild(d);
  chat.scrollTop = chat.scrollHeight;
}

function doSend() {
  var text = input.value.trim();
  if (!text) return;
  addMsg(text, true);
  input.value = '';
  send.disabled = true;
  send.textContent = '...';
  var x = new XMLHttpRequest();
  x.open('POST', '/argos/api', true);
  x.setRequestHeader('Content-Type', 'application/json');
  x.onload = function() {
    try {
      var r = JSON.parse(x.responseText);
      addMsg(r.result || 'Ok', false);
    } catch(e) { addMsg('Error', false); }
    send.disabled = false;
    send.textContent = '→';
  };
  x.onerror = function() {
    addMsg('Network error', false);
    send.disabled = false;
    send.textContent = '→';
  };
  x.send(JSON.stringify({method:'command', params:{text: text}}));
}

send.onclick = doSend;
input.onkeydown = function(e) { if (e.key == 'Enter') doSend(); };
addMsg('Ask anything. Type help for commands.', false);
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
