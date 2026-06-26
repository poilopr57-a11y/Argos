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
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>Argos</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:15px;
  background:var(--tg-theme-bg-color,#0b0f19);color:var(--tg-theme-text-color,#e8ecf4);padding:0;min-height:100vh;overflow-x:hidden;-webkit-overflow-scrolling:touch;user-select:none;-webkit-user-select:none}
.wrap{max-width:480px;margin:0 auto;padding:16px 16px 80px}
.card{background:var(--tg-theme-secondary-bg-color,#1a1f2e);border-radius:12px;padding:16px;margin:12px 0}
.card-title{font-size:15px;font-weight:600;margin-bottom:12px;display:flex;align-items:center;gap:8px}
.btn{display:block;width:100%;padding:14px;margin:8px 0;border:none;border-radius:10px;
  background:var(--tg-theme-button-color,#2563eb);color:var(--tg-theme-button-text-color,#fff);
  font-size:16px;font-weight:500;cursor:pointer;transition:opacity .15s;-webkit-tap-highlight-color:transparent;touch-action:manipulation}
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
.tab{flex:1;padding:12px 4px;text-align:center;font-size:11px;color:var(--tg-theme-hint-color,#8e94a2);cursor:pointer;transition:.15s;border:none;background:none;-webkit-tap-highlight-color:transparent;touch-action:manipulation}
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
<div class="wrap" id="app">
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
  <button class="tab active" data-tab="chat" onclick="switchTab('chat')"><span class="tab-icon">Chat</span>Chat</button>
  <button class="tab" data-tab="status" onclick="switchTab('status')"><span class="tab-icon">Status</span>Status</button>
  <button class="tab" data-tab="skills" onclick="switchTab('skills')"><span class="tab-icon">Skills</span>Skills</button>
  <button class="tab" data-tab="more" onclick="switchTab('more')"><span class="tab-icon">More</span>More</button>
</div>
<script>
var tg = window.Telegram.WebApp;
if (tg) tg.expand();

function $(id) { return document.getElementById(id); }

function addMsg(text, isUser) {
  var d = document.createElement('div');
  d.className = 'chat-msg ' + (isUser ? 'chat-user' : 'chat-bot');
  d.textContent = text;
  $('chatBox').appendChild(d);
  $('chatBox').scrollTop = $('chatBox').scrollHeight;
}

function sendCommand(text) {
  var xhr = new XMLHttpRequest();
  xhr.open('POST', '/argos/api', true);
  xhr.setRequestHeader('Content-Type', 'application/json');
  xhr.onload = function() {
    try {
      var data = JSON.parse(xhr.responseText);
      var reply = data.result || data.response || 'No response';
      addMsg(reply, false);
    } catch(e) { addMsg('Error parsing response', false); }
  };
  xhr.onerror = function() { addMsg('Network error', false); };
  xhr.send(JSON.stringify({method:'command', params:{text: text}}));
}

function doSend() {
  var input = $('chatInput');
  var text = input.value.trim();
  if (!text) return;
  addMsg(text, true);
  input.value = '';
  sendCommand(text);
}

$('chatSend').onclick = doSend;
$('chatInput').onkeydown = function(e) { if (e.key === 'Enter') doSend(); };

function switchTab(name) {
  var active = document.querySelectorAll('.tab');
  for (var j = 0; j < active.length; j++) active[j].classList.remove('active');
  var contents = document.querySelectorAll('.tab-content');
  for (var j = 0; j < contents.length; j++) contents[j].style.display = 'none';
  var tb = document.querySelector('[data-tab="' + name + '"]');
  if (tb) tb.classList.add('active');
  var tc = document.getElementById('tab-' + name);
  if (tc) tc.style.display = 'block';
  if (name === 'status') loadStatus();
  if (name === 'skills') loadActions();
  if (name === 'more') loadAbout();
}

function loadStatus() {
  $('systemStats').innerHTML = 'Loading...';
  var xhr = new XMLHttpRequest();
  xhr.open('POST', '/argos/api', true);
  xhr.setRequestHeader('Content-Type', 'application/json');
  xhr.onload = function() {
    try {
      var data = JSON.parse(xhr.responseText);
      $('systemStats').innerHTML = '<pre style="font-size:12px">' + (data.result || 'No data') + '</pre>';
    } catch(e) { $('systemStats').innerHTML = 'Error'; }
  };
  xhr.send(JSON.stringify({method:'command', params:{text:'status'}}));
  
  $('vpnStats').innerHTML = 'Loading...';
  var xhr2 = new XMLHttpRequest();
  xhr2.open('POST', '/argos/api', true);
  xhr2.setRequestHeader('Content-Type', 'application/json');
  xhr2.onload = function() {
    try {
      var data = JSON.parse(xhr2.responseText);
      $('vpnStats').innerHTML = '<pre style="font-size:12px">' + (data.result || 'No data') + '</pre>';
    } catch(e) { $('vpnStats').innerHTML = 'Error'; }
  };
  xhr2.send(JSON.stringify({method:'command', params:{text:'vpn'}}));
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
  for (var i = 0; i < actions.length; i++) {
    html += '<div class="grid-tile" onclick="quickCmd(\'' + actions[i].cmd + '\')"><div class="icon">' + actions[i].icon + '</div><div class="name">' + actions[i].name + '</div></div>';
  }
  $('actionsGrid').innerHTML = html;
}

function loadAbout() {
  $('aboutSection').innerHTML = 'Loading...';
  var xhr = new XMLHttpRequest();
  xhr.open('POST', '/argos/api', true);
  xhr.setRequestHeader('Content-Type', 'application/json');
  xhr.onload = function() {
    try {
      var data = JSON.parse(xhr.responseText);
      $('aboutSection').innerHTML = '<pre style="font-size:12px">' + (data.result || 'No data') + '</pre>';
    } catch(e) { $('aboutSection').innerHTML = 'Error'; }
  };
  xhr.send(JSON.stringify({method:'command', params:{text:'version'}}));
}

function quickCmd(cmd) {
  var tabs = document.querySelectorAll('.tab');
  for (var i = 0; i < tabs.length; i++) tabs[i].classList.remove('active');
  var contents = document.querySelectorAll('.tab-content');
  for (var i = 0; i < contents.length; i++) contents[i].style.display = 'none';
  document.querySelector('[data-tab="chat"]').classList.add('active');
  $('tab-chat').style.display = 'block';
  $('chatInput').value = cmd;
  doSend();
}

addMsg('Type help for commands or just ask anything.', false);
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
