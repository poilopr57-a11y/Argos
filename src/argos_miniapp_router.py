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
body{font-family:sans-serif;font-size:15px;background:var(--tg-theme-bg-color,#0b0f19);color:var(--tg-theme-text-color,#e8ecf4);height:100dvh;display:flex;flex-direction:column}
h2{padding:12px 16px 4px;font-size:14px;font-weight:600;color:var(--tg-theme-hint-color,#8e94a2)}
.card{background:var(--tg-theme-secondary-bg-color,#1a1f2e);border-radius:10px;padding:12px;margin:8px 16px}
.chat{flex:1;overflow-y:auto;padding:8px 16px}
.input-row{display:flex;gap:8px;padding:8px 16px 16px;background:var(--tg-theme-bg-color,#0b0f19)}
.input{flex:1;padding:12px 16px;border-radius:24px;border:1px solid rgba(255,255,255,.1);background:var(--tg-theme-secondary-bg-color,#1a1f2e);color:var(--tg-theme-text-color,#e8ecf4);font-size:14px;outline:none}
.input:focus{border-color:var(--tg-theme-button-color,#2563eb)}
.send{width:44px;height:44px;border-radius:50%;border:none;background:var(--tg-theme-button-color,#2563eb);color:#fff;font-size:18px;cursor:pointer;flex-shrink:0}
.send:active{opacity:.7}
.msg{padding:10px 14px;border-radius:12px;max-width:85%;font-size:14px;line-height:1.5;margin:4px 0;white-space:pre-wrap;word-break:break-word}
.msg-user{background:var(--tg-theme-button-color,#2563eb);color:#fff;align-self:flex-end;border-bottom-right-radius:4px}
.msg-bot{background:#2a3040;color:var(--tg-theme-text-color,#e8ecf4);align-self:flex-start;border-bottom-left-radius:4px}
.tabs{display:flex;border-bottom:1px solid rgba(255,255,255,.06);flex-shrink:0}
.tab{flex:1;padding:14px 4px;text-align:center;font-size:13px;font-weight:500;color:var(--tg-theme-hint-color,#8e94a2);cursor:pointer;border:none;background:none;border-bottom:2px solid transparent}
.tab.on{color:var(--tg-theme-button-color,#2563eb);border-bottom-color:var(--tg-theme-button-color,#2563eb)}
.hide{display:none}
.action{display:block;width:100%;padding:14px;margin:4px 0;border:none;border-radius:10px;text-align:left;font-size:14px;cursor:pointer;
 background:var(--tg-theme-secondary-bg-color,#1a1f2e);color:var(--tg-theme-text-color,#e8ecf4)}
.action:active{opacity:.6}
</style>
</head>
<body>
<div class="tabs">
<button class="tab on" id="tabChat" onclick="showTab('chat')">Чат</button>
<button class="tab" id="tabStatus" onclick="showTab('status')">Статус</button>
<button class="tab" id="tabSkills" onclick="showTab('skills')">Навыки</button>
<button class="tab" id="tabActions" onclick="showTab('actions')">Действия</button>
</div>
<div id="content" style="flex:1;display:flex;flex-direction:column;overflow:hidden">
<div id="view-chat" style="flex:1;display:flex;flex-direction:column">
<div class="chat" id="chat"></div>
<div class="input-row">
<input class="input" id="input" placeholder="Спроси что угодно..." />
<button class="send" id="send">↑</button>
</div>
</div>
<div id="view-status" class="hide" style="padding:8px;overflow-y:auto">
<h2>Система</h2><div class="card" id="statusOut"></div>
<h2>Провайдеры</h2><div class="card" id="providersOut"></div>
<h2>GPU</h2><div class="card" id="gpuOut"></div>
</div>
<div id="view-skills" class="hide" style="padding:8px;overflow-y:auto">
<div id="skillsOut"></div>
</div>
<div id="view-actions" class="hide" style="padding:8px">
<div id="actionsOut"></div>
</div>
</div>
<script>
var W = window.Telegram.WebApp;
if (W) { W.ready(); W.expand(); }

function $(id) { return document.getElementById(id); }

// ---- CHAT ----
var chat = $('chat'), inp = $('input'), btn = $('send');
function rpc(text, cb) {
  var x = new XMLHttpRequest();
  x.open('POST', '/argos/api', true);
  x.setRequestHeader('Content-Type', 'application/json');
  x.onload = function() {
    try { cb(JSON.parse(x.responseText).result || 'OK'); }
    catch(e) { cb('Error'); }
  };
  x.onerror = function() { cb('Network error'); };
  x.timeout = 15000;
  x.ontimeout = function() { cb('Таймаут'); x.abort(); };
  x.send(JSON.stringify({method:'command', params:{text:text}}));
}
function addMsg(t, user) {
  var d = document.createElement('div');
  d.className = 'msg msg-' + (user ? 'user' : 'bot');
  d.textContent = t;
  chat.appendChild(d);
  chat.scrollTop = chat.scrollHeight;
}
function doSend() {
  var t = inp.value.trim(); if (!t) return;
  addMsg(t, true); inp.value = ''; btn.disabled = true;
  rpc(t, function(r) { addMsg(r, false); btn.disabled = false; });
}
btn.onclick = doSend;
inp.onkeydown = function(e) { if (e.key == 'Enter') doSend(); };

// ---- TABS ----
var views = ['chat','status','skills','actions'];
function showTab(name) {
  for (var i = 0; i < views.length; i++) {
    $('tab'+views[i].charAt(0).toUpperCase()+views[i].slice(1)).className = 'tab';
    var v = $('view-' + views[i]);
    if (v) { v.className = 'hide'; }
  }
  $('tab'+name.charAt(0).toUpperCase()+name.slice(1)).className = 'tab on';
  var v = $('view-' + name);
  if (v) { v.className = ''; v.style.flex = '1'; v.style.display = 'flex'; }
  if (name == 'status') loadStatus();
  if (name == 'skills') loadSkills();
  if (name == 'actions') loadActions();
}

// ---- STATUS ----
function loadStatus() {
  rpc('mcp debug', function(r) { $('statusOut').innerHTML = '<pre style="font-size:12px;white-space:pre-wrap">'+r+'</pre>'; });
  rpc('providers', function(r) { $('providersOut').innerHTML = '<pre style="font-size:12px;white-space:pre-wrap">'+r+'</pre>'; });
  rpc('gpu status', function(r) { $('gpuOut').innerHTML = '<pre style="font-size:12px;white-space:pre-wrap">'+r+'</pre>'; });
}

// ---- SKILLS ----
function loadSkills() {
  rpc('список навыков', function(r) {
    $('skillsOut').innerHTML = '<pre style="font-size:12px;white-space:pre-wrap;line-height:1.3">'+r+'</pre>';
  });
}

// ---- ACTIONS ----
function loadActions() {
  var cmds = [
    ['💻 Система','статус системы'],['🎮 GPU','gpu status'],['🌐 P2P','p2p status'],
    ['🧠 Память','mempalace status'],['📝 Obsidian','obsidian status'],['✉️ Telegram','telegram status'],
    ['🤖 AI','providers'],['❓ Помощь','помощь']
  ];
  var h = '';
  for (var i = 0; i < cmds.length; i++) {
    h += '<button class="action" onclick="runCmd(\'' + cmds[i][1] + '\')">' + cmds[i][0] + '</button>';
  }
  $('actionsOut').innerHTML = h;
}

function runCmd(cmd) {
  showTab('chat');
  inp.value = cmd; doSend();
}

addMsg('ARGOS v2.1.3 готов. Спрашивай!', false);
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
