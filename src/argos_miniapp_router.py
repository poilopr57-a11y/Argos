from __future__ import annotations

import json
import os
from typing import Any

import aiohttp
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter(prefix="/argos", tags=["argos"])

MINIAPP_VERSION = "1.0.0"

MCP_TARGET = os.getenv("ARGOS_MCP_TARGET", "http://127.0.0.1:8000/mcp")


async def _mcp_call(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params or {},
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(MCP_TARGET, json=body, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                data = await resp.json()
                return data
    except Exception as exc:
        return {"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": str(exc)}}


def _webapp_html() -> str:
    webapp_url = os.getenv("ARGOS_VPN_WEBAPP_URL", "").rstrip("/") + "/argos"
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>Argos</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
*{{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:15px;
  background:var(--tg-theme-bg-color,#0b0f19);color:var(--tg-theme-text-color,#e8ecf4);padding:0;min-height:100vh;overflow-x:hidden}}
.wrap{{max-width:480px;margin:0 auto;padding:16px 16px 80px}}
.card{{background:var(--tg-theme-secondary-bg-color,#1a1f2e);border-radius:12px;padding:16px;margin:12px 0}}
.card-title{{font-size:15px;font-weight:600;margin-bottom:12px;display:flex;align-items:center;gap:8px}}
.btn{{display:block;width:100%;padding:14px;margin:8px 0;border:none;border-radius:10px;
  background:var(--tg-theme-button-color,#2563eb);color:var(--tg-theme-button-text-color,#fff);
  font-size:16px;font-weight:500;cursor:pointer;transition:opacity .15s}}
.btn:active{{opacity:.7}}.btn:disabled{{opacity:.4;cursor:not-allowed}}
.btn-sm{{padding:10px;font-size:13px;width:auto;display:inline-block;margin:4px}}
.btn-outline{{background:transparent;border:1px solid var(--tg-theme-button-color,#2563eb);color:var(--tg-theme-button-color,#2563eb)}}
.row{{display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-bottom:1px solid rgba(255,255,255,.06)}}
.row:last-child{{border-bottom:none}}
.label{{color:var(--tg-theme-hint-color,#8e94a2);font-size:13px}}
.value{{font-size:14px;font-weight:500;text-align:right}}
.badge{{display:inline-block;padding:2px 10px;border-radius:20px;font-size:12px;font-weight:600}}
.badge-ok{{background:#16a34a33;color:#16a34a}}
.badge-warn{{background:#d9770633;color:#d97706}}
.badge-off{{background:#6b728033;color:#6b7280}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:12px 0}}
.grid-tile{{background:var(--tg-theme-secondary-bg-color,#1a1f2e);border-radius:10px;padding:14px;text-align:center;cursor:pointer;transition:.15s}}
.grid-tile:active{{opacity:.6}}
.grid-tile .icon{{font-size:24px;margin-bottom:4px}}
.grid-tile .name{{font-size:12px;color:var(--tg-theme-hint-color,#8e94a2)}}
.tabs{{display:flex;position:fixed;bottom:0;left:0;right:0;background:var(--tg-theme-bg-color,#0b0f19);border-top:1px solid rgba(255,255,255,.06);z-index:100;max-width:480px;margin:0 auto}}
.tab{{flex:1;padding:12px 4px;text-align:center;font-size:11px;color:var(--tg-theme-hint-color,#8e94a2);cursor:pointer;transition:.15s;border:none;background:none}}
.tab.active{{color:var(--tg-theme-button-color,#2563eb);font-weight:600}}
.tab .tab-icon{{font-size:20px;display:block;margin-bottom:2px}}
.chat-box{{height:50vh;overflow-y:auto;padding:12px;border-radius:10px;background:var(--tg-theme-secondary-bg-color,#1a1f2e);margin:12px 0;display:flex;flex-direction:column;gap:8px}}
.chat-msg{{padding:10px 14px;border-radius:12px;max-width:85%;word-break:break-word;font-size:14px;line-height:1.4}}
.chat-user{{background:var(--tg-theme-button-color,#2563eb);color:#fff;align-self:flex-end;border-bottom-right-radius:4px}}
.chat-bot{{background:#2a3040;color:var(--tg-theme-text-color,#e8ecf4);align-self:flex-start;border-bottom-left-radius:4px}}
.chat-input-row{{display:flex;gap:8px;align-items:center}}
.chat-input{{flex:1;padding:12px 16px;border-radius:24px;border:1px solid rgba(255,255,255,.1);background:var(--tg-theme-secondary-bg-color,#1a1f2e);color:var(--tg-theme-text-color,#e8ecf4);font-size:14px;outline:none}}
.chat-input:focus{{border-color:var(--tg-theme-button-color,#2563eb)}}
.loader{{text-align:center;padding:20px;color:var(--tg-theme-hint-color,#8e94a2)}}
.empty-state{{text-align:center;padding:30px;color:var(--tg-theme-hint-color,#8e94a2);font-size:14px}}
</style>
</head>
<body>
<div class="wrap" id="app" style="display:none">
  <div id="tab-chat" class="tab-content">
    <div class="card-title"><span>Чат с ИИ</span></div>
    <div class="chat-box" id="chatBox"></div>
    <div class="chat-input-row">
      <input class="chat-input" id="chatInput" placeholder="Напишите сообщение..." />
      <button class="btn btn-sm" id="chatSend" style="width:auto;padding:12px 20px;border-radius:24px">→</button>
    </div>
  </div>
  <div id="tab-status" class="tab-content" style="display:none">
    <div class="card-title"><span>Система</span></div>
    <div class="card"><div id="systemStats"></div></div>
    <div class="card-title" style="margin-top:16px"><span>Провайдеры</span></div>
    <div class="card"><div id="providerStats"></div></div>
  </div>
  <div id="tab-skills" class="tab-content" style="display:none">
    <div class="card-title"><span>Навыки</span></div>
    <div id="skillsList" class="grid"></div>
  </div>
  <div id="tab-more" class="tab-content" style="display:none">
    <div class="card-title"><span>Действия</span></div>
    <div class="grid" id="actionsGrid"></div>
    <div class="card-title" style="margin-top:16px"><span>О системе</span></div>
    <div class="card" id="aboutSection"></div>
  </div>
</div>
<div class="tabs" id="tabs">
  <button class="tab active" data-tab="chat"><span class="tab-icon">💬</span>Чат</button>
  <button class="tab" data-tab="status"><span class="tab-icon">📊</span>Статус</button>
  <button class="tab" data-tab="skills"><span class="tab-icon">🧠</span>Навыки</button>
  <button class="tab" data-tab="more"><span class="tab-icon">⚙️</span>Ещё</button>
</div>
<div class="loader" id="loader">Загрузка...</div>
<script>
const API = '/argos/api';
const tg = window.Telegram.WebApp;
tg.expand();
tg.enableClosingConfirmation();

const $ = id => document.getElementById(id);
const chatBox = $('chatBox'), chatInput = $('chatInput'), chatSend = $('chatSend');

async function mcp(method, params) {{
  try {{
    const r = await fetch(API, {{
      method:'POST', headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{jsonrpc:'2.0', id:1, method, params}})
    }});
    return await r.json();
  }} catch(e) {{ return {{error:{{message:e.message}}}} }}
}}

function addChatMsg(text, isUser) {{
  const d = document.createElement('div');
  d.className = 'chat-msg ' + (isUser ? 'chat-user' : 'chat-bot');
  d.textContent = text;
  chatBox.appendChild(d);
  chatBox.scrollTop = chatBox.scrollHeight;
}}

chatSend.onclick = async () => {{
  const text = chatInput.value.trim();
  if (!text) return;
  addChatMsg(text, true);
  chatInput.value = '';
  chatSend.disabled = true;
  chatSend.textContent = '...';
  try {{
    const r = await mcp('tools/call', {{name:'command', arguments:{{text}}}});
    const reply = r?.result?.content?.[0]?.text || r?.error?.message || 'Нет ответа';
    addChatMsg(reply, false);
  }} catch(e) {{ addChatMsg('Ошибка: '+e.message, false) }}
  finally {{ chatSend.disabled = false; chatSend.textContent = '→' }}
}};
chatInput.onkeydown = e => {{ if (e.key === 'Enter') chatSend.click(); }};

// Tabs
document.querySelectorAll('.tab').forEach(tab => {{
  tab.onclick = () => {{
    document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(t=>t.style.display='none');
    tab.classList.add('active');
    $(`tab-${{tab.dataset.tab}}`).style.display = 'block';
    if (tab.dataset.tab === 'status') loadStatus();
    if (tab.dataset.tab === 'skills') loadSkills();
    if (tab.dataset.tab === 'more') loadMore();
  }}
}});

async function loadStatus() {{
  $('systemStats').innerHTML = '<div class="loader">Загрузка...</div>';
  const r = await mcp('tools/call', {{name:'command', arguments:{{text:'mcp debug'}}}});
  const text = r?.result?.content?.[0]?.text || 'Нет данных';
  $('systemStats').innerHTML = '<pre style="font-size:12px;white-space:pre-wrap;color:var(--tg-theme-hint-color,#8e94a2)">'+text+'</pre>';
  $('providerStats').innerHTML = '<div class="loader">Загрузка...</div>';
  const r2 = await mcp('tools/call', {{name:'command', arguments:{{text:'providers'}}}});
  const txt2 = r2?.result?.content?.[0]?.text || 'Нет данных';
  $('providerStats').innerHTML = '<pre style="font-size:12px;white-space:pre-wrap;color:var(--tg-theme-hint-color,#8e94a2)">'+txt2+'</pre>';
}}

async function loadSkills() {{
  $('skillsList').innerHTML = '<div class="loader">Загрузка...</div>';
  const r = await mcp('tools/call', {{name:'command', arguments:{{text:'skills'}}}});
  const txt = r?.result?.content?.[0]?.text || '';
  const skills = txt.split('\\n').filter(s => s.trim()).slice(0, 20);
  $('skillsList').innerHTML = skills.map(s => `<div class="grid-tile" onclick="chatAsk('${{s.replace(/['"]/g,'')}}')"><span class="name">${{s.slice(0,40)}}</span></div>`).join('');
}}

async function loadMore() {{
  const actions = [
    {{name:'Система', emoji:'💻', cmd:'статус системы'}},
    {{name:'GPU', emoji:'🎮', cmd:'gpu status'}},
    {{name:'P2P', emoji:'🌐', cmd:'p2p status'}},
    {{name:'Memory', emoji:'🧠', cmd:'mempalace status'}},
    {{name:'Obsidian', emoji:'📝', cmd:'obsidian status'}},
    {{name:'Telegram', emoji:'✉️', cmd:'telegram status'}},
    {{name:'Навыки', emoji:'📋', cmd:'список навыков'}},
    {{name:'Помощь', emoji:'❓', cmd:'помощь'}},
  ];
  $('actionsGrid').innerHTML = actions.map(a =>
    `<div class="grid-tile" onclick="chatAsk('${{a.cmd}}')"><div class="icon">${{a.emoji}}</div><div class="name">${{a.name}}</div></div>`
  ).join('');
  $('aboutSection').innerHTML = '<div class="row"><span class="label">Версия</span><span class="value">ARGOS v2.1.3</span></div>' +
    '<div class="row"><span class="label">Mini-App</span><span class="value">v{MINIAPP_VERSION}</span></div>';
}}

function chatAsk(cmd) {{
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(t=>t.style.display='none');
  document.querySelector('[data-tab="chat"]').classList.add('active');
  $('tab-chat').style.display = 'block';
  chatInput.value = cmd;
  chatSend.click();
}}

    (async function(){{
  const r = await mcp('tools/call', {{name:'command', arguments:{{text:'+ ping'}}}});
  if (r?.result?.content?.[0]?.text) {{
    $('loader').style.display = 'none';
    $('app').style.display = 'block';
    addChatMsg('✅ ARGOS готов. Спрашивай что угодно!', false);
  }} else {{
    $('loader').innerHTML = 'Ошибка подключения к ARGOS';
  }}
}})();
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
        return JSONResponse(status_code=400, content={"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid JSON"}})

    result = await _mcp_call(method, params)
    return JSONResponse(content=result)


@router.get("/health")
async def argos_health():
    result = await _mcp_call("tools/list")
    return {"status": "ok", "version": MINIAPP_VERSION, "mcp_connected": "error" not in result}
