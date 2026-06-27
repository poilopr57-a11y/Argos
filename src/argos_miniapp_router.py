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

    # Short messages get local response
    if len(t) <= 20 and any(c.isdigit() for c in t):
        return f"Код получен. Для AI-ответа напиши развёрнутый вопрос."
    if t in ("привет", "hi", "hello", "ку"):
        return "Привет! Спрашивай что угодно — ARGOS ответит."

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
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                data = await resp.json()
                reply = data.get("result", {}).get("content", [{}])[0].get("text", "")
                if reply:
                    return reply
                return "MCP вернул пустой ответ"
    except Exception as exc:
        return f"Жди... MCP отвечает ({str(exc)[:50]})"

    return "Готов"


def _webapp_html() -> str:
    return """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>Argos</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;font-size:14px;
  background:var(--tg-theme-bg-color,#0b0f19);color:var(--tg-theme-text-color,#e8ecf4);padding:0;min-height:100vh}
.wrap{max-width:480px;margin:0 auto;padding:16px 16px 80px}
.card{background:var(--tg-theme-secondary-bg-color,#1a1f2e);border-radius:12px;padding:16px;margin:12px 0}
.btn{display:block;width:100%;padding:14px;margin:8px 0;border:none;border-radius:10px;
  background:var(--tg-theme-button-color,#2563eb);color:var(--tg-theme-button-text-color,#fff);font-size:15px;cursor:pointer}
.btn:active{opacity:.7}
.pre{font-size:12px;white-space:pre-wrap;line-height:1.4;color:var(--tg-theme-hint-color,#8e94a2)}
.chat-box{max-height:50vh;overflow-y:auto;padding:8px;margin:12px 0;border-radius:10px;background:var(--tg-theme-secondary-bg-color,#1a1f2e)}
.msg{padding:8px 12px;border-radius:10px;margin:6px 0;font-size:14px;line-height:1.4;word-break:break-word;white-space:pre-wrap}
.msg-u{background:var(--tg-theme-button-color,#2563eb);color:#fff;border-bottom-right-radius:3px}
.msg-b{background:#252a3a;color:var(--tg-theme-text-color,#e8ecf4);border-bottom-left-radius:3px}
.chat-input{display:flex;gap:8px}
.chat-input input{flex:1;padding:12px 16px;border-radius:24px;border:1px solid rgba(255,255,255,.1);
  background:var(--tg-theme-secondary-bg-color,#1a1f2e);color:var(--tg-theme-text-color,#e8ecf4);font-size:14px;outline:none}
.chat-input button{width:44px;height:44px;border-radius:50%;border:none;background:var(--tg-theme-button-color,#2563eb);color:#fff;font-size:18px;cursor:pointer}
.quick{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:12px 0}
.quick-btn{background:var(--tg-theme-secondary-bg-color,#1a1f2e);border:1px solid rgba(255,255,255,.06);border-radius:10px;padding:12px;text-align:center;cursor:pointer;font-size:13px;color:var(--tg-theme-hint-color,#8e94a2)}
.quick-btn:active{opacity:.6}
</style>
</head>
<body>
<div class="wrap">
<div style="display:flex;gap:4px;margin-bottom:12px">
<button class="tab" data-tab="chat" style="border:none;background:none;color:var(--tg-theme-hint-color);padding:10px 14px;font-size:14px;border-bottom:2px solid transparent">Чат</button>
<button class="tab" data-tab="status" style="border:none;background:none;color:var(--tg-theme-hint-color);padding:10px 14px;font-size:14px;border-bottom:2px solid transparent">Статус</button>
<button class="tab" data-tab="skills" style="border:none;background:none;color:var(--tg-theme-hint-color);padding:10px 14px;font-size:14px;border-bottom:2px solid transparent">Навыки</button>
<button class="tab" data-tab="actions" style="border:none;background:none;color:var(--tg-theme-hint-color);padding:10px 14px;font-size:14px;border-bottom:2px solid transparent">Ещё</button>
</div>

<div class="tab-content" id="tab-chat" style="display:block">
<div class="chat-box" id="chat" style="min-height:120px"><div class="msg msg-b">ARGOS v2.1.3. Спроси что угодно.</div></div>
<div class="chat-input"><input id="inp" placeholder="Вопрос или команда..."><button id="send">↑</button></div>
</div>

<div class="tab-content" id="tab-status" style="display:none">
<div class="card"><div class="pre" id="statusOut">Загрузка...</div></div>
<div class="card"><div class="pre" id="providersOut">Загрузка...</div></div>
<div class="card"><div class="pre" id="gpuOut">Загрузка...</div></div>
</div>

<div class="tab-content" id="tab-skills" style="display:none">
<div class="pre" id="skillsOut">Загрузка...</div>
</div>

<div class="tab-content" id="tab-actions" style="display:none">
<div class="quick" id="actionsOut"></div>
</div>
</div>

<script>
var W=window.Telegram.WebApp;W.ready();W.expand();
function $(id){return document.getElementById(id)}
var ch=$('chat'),inp=$('inp'),btn=$('send');

function addMsg(t,u){var d=document.createElement('div');d.className='msg '+(u?'msg-u':'msg-b');d.textContent=t;ch.appendChild(d);ch.scrollTop=ch.scrollHeight}
function api(text,cb){var x=new XMLHttpRequest();x.open('POST','/argos/api',true);x.setRequestHeader('Content-Type','application/json');x.timeout=25000;x.onload=function(){try{cb(JSON.parse(x.responseText).result||'Ok')}catch(e){cb('Error')}};x.onerror=function(){cb('Network error')};x.ontimeout=function(){cb('Timeout');x.abort()};x.send(JSON.stringify({method:'command',params:{text:text}}))}
function doSend(){var t=inp.value.trim();if(!t)return;addMsg(t,true);inp.value='';btn.disabled=true;api(t,function(r){addMsg(r,false);btn.disabled=false})}
btn.onclick=doSend;inp.onkeydown=function(e){if(e.key=='Enter')doSend()}

document.querySelectorAll('.tab').forEach(function(tab){tab.onclick=function(){
  document.querySelectorAll('.tab').forEach(function(t){t.style.borderBottomColor='transparent';t.style.color='var(--tg-theme-hint-color)'});
  document.querySelectorAll('.tab-content').forEach(function(t){t.style.display='none'});
  tab.style.borderBottomColor='var(--tg-theme-button-color,#2563eb)';tab.style.color='var(--tg-theme-text-color)';
  $('tab-'+tab.dataset.tab).style.display='block';
  if(tab.dataset.tab=='status'){api('mcp debug',function(r){$('statusOut').textContent=r});api('providers',function(r){$('providersOut').textContent=r});api('gpu status',function(r){$('gpuOut').textContent=r})}
  if(tab.dataset.tab=='skills'){api('список навыков',function(r){$('skillsOut').textContent=r})}
  if(tab.dataset.tab=='actions'){var c=[['Система','статус системы'],['GPU','gpu status'],['P2P','p2p status'],['Память','mempalace status'],['Obsidian','obsidian status'],['Telegram','telegram status'],['ИИ','providers'],['VPN','vpn status']];var h='';for(var i=0;i<c.length;i++){h+='<div class=\"quick-btn\" onclick=\"var t=document.querySelectorAll(\\'.tab\\');t[0].click();inp.value=\\''+c[i][1]+'\\';doSend()\">'+c[i][0]+'</div>'};$('actionsOut').innerHTML=h}
  W.expand()}})
var ft=document.querySelector('.tab');if(ft){ft.style.borderBottomColor='var(--tg-theme-button-color,#2563eb)';ft.style.color='var(--tg-theme-text-color)'}
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
