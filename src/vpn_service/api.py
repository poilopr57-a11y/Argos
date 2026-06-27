"""FastAPI routes for Argos VPN client management."""

from __future__ import annotations

import base64
import io
import os
import time
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field, field_validator

from src.vpn_service.database import Database
from src.vpn_service.wg_manager import WireGuardManager


class RegisterRequest(BaseModel):
    telegram_id: int = Field(..., gt=0)
    username: Optional[str] = Field(None, max_length=64)


class ClientConfigRequest(BaseModel):
    telegram_id: int = Field(..., gt=0)


class ExtendRequest(BaseModel):
    telegram_id: int = Field(..., gt=0)
    days: int = Field(default=3, ge=1, le=90)


class AdminCleanupResponse(BaseModel):
    deactivated: int
    public_keys: list[str]


router = APIRouter(prefix="/vpn", tags=["vpn"])


SERVERS = [
    {"id": "eu-west", "name": "Europe West", "flag": "🇪🇺", "location": "Amsterdam, NL", "ping": 45, "load": 32},
    {"id": "eu-east", "name": "Europe East", "flag": "🇪🇺", "location": "Frankfurt, DE", "ping": 52, "load": 28},
    {"id": "us-east", "name": "US East", "flag": "🇺🇸", "location": "New York, US", "ping": 112, "load": 45},
    {"id": "us-west", "name": "US West", "flag": "🇺🇸", "location": "Los Angeles, US", "ping": 145, "load": 38},
    {"id": "asia", "name": "Asia Pacific", "flag": "🌏", "location": "Singapore", "ping": 180, "load": 22},
]


def _webapp_html() -> str:
    return """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>Argos VPN</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<script src="https://cdn.jsdelivr.net/npm/qrcodejs@1.0.0/qrcode.min.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:15px;
  background:var(--tg-theme-bg-color,#0b0f19);
  color:var(--tg-theme-text-color,#e8ecf4);padding:0;min-height:100vh;overflow-x:hidden}
.wrap{max-width:480px;margin:0 auto;padding:16px 16px 80px}
.card{background:var(--tg-theme-secondary-bg-color,#1a1f2e);border-radius:12px;padding:16px;margin:12px 0}
.card-title{font-size:15px;font-weight:600;margin-bottom:12px;display:flex;align-items:center;gap:8px}
.btn{display:block;width:100%;padding:14px;margin:8px 0;border:none;border-radius:10px;
  background:var(--tg-theme-button-color,#2563eb);color:var(--tg-theme-button-text-color,#fff);
  font-size:16px;font-weight:500;cursor:pointer;transition:opacity .15s}
.btn:active{opacity:.7}.btn:disabled{opacity:.4;cursor:not-allowed}
.btn-sm{padding:10px;font-size:13px;width:auto;display:inline-block;margin:4px}
.btn-outline{background:transparent;border:1px solid var(--tg-theme-button-color,#2563eb);
  color:var(--tg-theme-button-color,#2563eb)}
.btn-danger{background:#dc2626}
.btn-success{background:#16a34a}
.row{display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-bottom:1px solid rgba(255,255,255,.06)}
.row:last-child{border-bottom:none}
.label{color:var(--tg-theme-hint-color,#8e94a2);font-size:13px}
.value{font-size:14px;font-weight:500;text-align:right}
.badge{display:inline-block;padding:2px 10px;border-radius:20px;font-size:12px;font-weight:600}
.badge-ok{background:#16a34a22;color:#4ade80}
.badge-warn{background:#ea580c22;color:#fb923c}
.badge-exp{background:#dc262622;color:#f87171}
.config-box{background:#0d1117;border-radius:8px;padding:12px;font-family:'SF Mono',Consolas,monospace;
  font-size:11px;line-height:1.5;overflow-x:auto;white-space:pre-wrap;word-break:break-all;
  max-height:250px;overflow-y:auto;user-select:all;margin:8px 0}
.tabs{display:flex;position:sticky;top:0;z-index:10;background:var(--tg-theme-bg-color,#0b0f19);
  padding:8px 0 0;border-bottom:1px solid rgba(255,255,255,.08);backdrop-filter:blur(8px)}
.tab{flex:1;text-align:center;padding:10px 4px;font-size:13px;font-weight:500;
  color:var(--tg-theme-hint-color,#8e94a2);cursor:pointer;border-bottom:2px solid transparent;
  transition:all .2s;user-select:none;position:relative}
.tab.active{color:var(--tg-theme-button-color,#2563eb);border-bottom-color:var(--tg-theme-button-color,#2563eb)}
.tab .tab-icon{font-size:18px;display:block;margin-bottom:2px}
.tab-content{display:none}.tab-content.active{display:block}
.qr-wrap{display:flex;justify-content:center;padding:16px 0}
.qr-wrap canvas,.qr-wrap img{border-radius:8px;background:#fff}
.toast{position:fixed;bottom:80px;left:50%;transform:translateX(-50%);
  background:var(--tg-theme-button-color,#2563eb);color:var(--tg-theme-button-text-color,#fff);
  padding:10px 24px;border-radius:10px;font-size:14px;font-weight:500;
  opacity:0;transition:opacity .3s;pointer-events:none;z-index:99;white-space:nowrap}
.toast.show{opacity:1}
.loader{text-align:center;padding:60px 0;color:var(--tg-theme-hint-color,#8e94a2)}
.loader .spinner{width:32px;height:32px;border:3px solid rgba(255,255,255,.1);
  border-top-color:var(--tg-theme-button-color,#2563eb);border-radius:50%;
  animation:spin .8s linear infinite;margin:0 auto 12px}
@keyframes spin{to{transform:rotate(360deg)}}
.header{text-align:center;padding:16px 0 4px}
.header h1{font-size:24px;font-weight:700}
.header p{color:var(--tg-theme-hint-color,#8e94a2);font-size:13px;margin-top:2px}
.empty-state{text-align:center;padding:30px 0;color:var(--tg-theme-hint-color,#8e94a2)}
.empty-state .empty-icon{font-size:48px;margin-bottom:12px;opacity:.5}
.chart-bar{display:flex;align-items:flex-end;height:100px;gap:4px;padding:8px 0}
.chart-bar-item{flex:1;border-radius:4px 4px 0 0;min-height:4px;transition:height .5s;
  background:var(--tg-theme-button-color,#2563eb);opacity:.7}
.chart-bar-item.active{opacity:1}
.chart-labels{display:flex;gap:4px;font-size:10px;color:var(--tg-theme-hint-color,#8e94a2)}
.chart-labels span{flex:1;text-align:center}
.server-card{display:flex;align-items:center;gap:12px;padding:12px;margin:6px 0;
  background:rgba(255,255,255,.03);border-radius:10px;cursor:pointer;transition:background .2s;
  border:1px solid transparent}
.server-card:active{background:rgba(255,255,255,.06)}
.server-card.selected{border-color:var(--tg-theme-button-color,#2563eb)}
.server-card .server-flag{font-size:24px}
.server-card .server-info{flex:1}
.server-card .server-name{font-size:14px;font-weight:500}
.server-card .server-loc{font-size:12px;color:var(--tg-theme-hint-color,#8e94a2)}
.server-card .server-meta{text-align:right;font-size:12px;color:var(--tg-theme-hint-color,#8e94a2)}
.pulse{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:6px}
.pulse-green{background:#4ade80;box-shadow:0 0 6px #4ade8066}
.pulse-yellow{background:#fb923c;box-shadow:0 0 6px #fb923c66}
.pulse-red{background:#f87171;box-shadow:0 0 6px #f8717166}
.modal-overlay{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.6);
  z-index:100;display:none;align-items:flex-end;justify-content:center}
.modal-overlay.show{display:flex}
.modal{background:var(--tg-theme-secondary-bg-color,#1a1f2e);border-radius:16px 16px 0 0;
  padding:20px;width:100%;max-width:480px;max-height:70vh;overflow-y:auto}
.modal-title{font-size:17px;font-weight:600;margin-bottom:16px}
.modal .btn{margin:6px 0}
.action-row{display:flex;gap:8px;margin-top:8px}
.action-row .btn{flex:1}
</style>
</head>
<body>

<div id="loader" class="loader"><div class="spinner"></div><p>Загрузка...</p></div>
<div id="errorBlock" class="card" style="display:none;text-align:center;margin:40px 16px">
  <p style="font-size:40px;margin-bottom:12px">⚠️</p>
  <p style="color:#f87171;font-size:15px" id="errorText"></p>
  <button class="btn" onclick="location.reload()" style="margin-top:16px">Обновить</button>
</div>

<div id="app" style="display:none">
  <div class="header">
    <h1>Argos VPN</h1>
    <p>Безопасный и анонимный VPN-доступ</p>
  </div>

  <div class="tabs" id="tabs">
    <div class="tab active" data-tab="dashboard"><span class="tab-icon">📊</span>Статус</div>
    <div class="tab" data-tab="config"><span class="tab-icon">🔐</span>Конфиг</div>
    <div class="tab" data-tab="servers"><span class="tab-icon">🌍</span>Серверы</div>
    <div class="tab" data-tab="profile"><span class="tab-icon">👤</span>Профиль</div>
  </div>

  <div class="wrap">

    <!-- ===== DASHBOARD ===== -->
    <div class="tab-content active" id="tab-dashboard">
      <div class="card">
        <div id="dashStatus"></div>
      </div>

      <div class="card">
        <div class="card-title">📈 Использование трафика</div>
        <canvas id="trafficChart" width="440" height="100" style="width:100%;height:100px;border-radius:4px;"></canvas>
        <div style="text-align:center;margin-top:8px">
          <span style="font-size:24px;font-weight:700" id="dashTrafficGb">0</span>
          <span style="color:var(--tg-theme-hint-color,#8e94a2);font-size:13px"> GB</span>
          <span style="color:var(--tg-theme-hint-color,#8e94a2);font-size:13px"> / 5 GB</span>
        </div>
      </div>

      <div class="card">
        <div class="card-title">⚡ Быстрые действия</div>
        <button class="btn" id="dashGetConfig">Получить VPN-конфиг</button>
        <button class="btn btn-outline" id="dashExtend">Продлить подписку</button>
      </div>
    </div>

    <!-- ===== CONFIG ===== -->
    <div class="tab-content" id="tab-config">
      <div class="card" id="configNoConfig">
        <div class="empty-state">
          <div class="empty-icon">🔐</div>
          <p style="font-size:15px;font-weight:500;margin-bottom:8px">Нет активного конфига</p>
          <p style="font-size:13px;color:var(--tg-theme-hint-color,#8e94a2);margin-bottom:16px">
            Создайте конфигурацию для подключения к VPN
          </p>
          <button class="btn" id="configCreateBtn">Создать конфиг</button>
        </div>
      </div>

      <div class="card" style="margin-top:16px">
        <div class="card-title"><span>⚡ VLESS+WS (рекомендуется)</span></div>
        <p style="font-size:12px;color:#16a34a;margin-bottom:8px">Работает в России • через Cloudflare</p>
        <pre class="config-box" style="font-size:11px;max-height:60px;overflow:hidden" id="vlessText">vless://bcdae1c0-93ab-49b1-b3a8-8465b982f888@vpn.argosssss.win:443?encryption=none&security=tls&sni=vpn.argosssss.win&type=ws&path=%2Fray&host=vpn.argosssss.win#Argos-CF-WS</pre>
        <div class="action-row">
          <button class="btn btn-sm" id="vlessCopyBtn">📋 Копировать</button>
          <button class="btn btn-sm" onclick="window.open('/vpn/qr_vless')">📱 QR</button>
        </div>
      </div>

      <div id="configExists" style="display:none">
        <div class="card">
          <div class="card-title">
            <span>📋 Ваш конфиг</span>
            <span class="badge badge-ok" id="configStatusBadge">Активен</span>
          </div>

          <div class="qr-wrap" id="qrContainer"></div>

          <pre class="config-box" id="configText"></pre>

          <div class="action-row">
            <button class="btn btn-sm" id="configCopyBtn">📋 Копировать</button>
            <button class="btn btn-sm" id="configDownloadBtn">💾 Скачать</button>
            <button class="btn btn-sm btn-danger" id="configDeleteBtn">🗑️ Удалить</button>
          </div>
        </div>

        <div class="card">
          <div class="card-title">⏱ Информация</div>
          <div id="configInfo"></div>
        </div>
      </div>

      <!-- Extend modal -->
      <div class="modal-overlay" id="extendModal">
        <div class="modal">
          <div class="modal-title">🔄 Продлить подписку</div>
          <p style="font-size:13px;color:var(--tg-theme-hint-color,#8e94a2);margin-bottom:16px">
            Выберите срок продления
          </p>
          <button class="btn btn-sm" onclick="extendDays(3)">3 дня</button>
          <button class="btn btn-sm" onclick="extendDays(7)">7 дней</button>
          <button class="btn btn-sm" onclick="extendDays(30)">30 дней</button>
          <button class="btn btn-sm btn-outline" onclick="closeExtendModal()">Отмена</button>
        </div>
      </div>
    </div>

    <!-- ===== SERVERS ===== -->
    <div class="tab-content" id="tab-servers">
      <div class="card">
        <div class="card-title">🌍 Выберите сервер</div>
        <div id="serverList"></div>
      </div>
      <div class="card">
        <div class="card-title">📊 Статус сети</div>
        <div id="networkStatus"></div>
      </div>
    </div>

    <!-- ===== PROFILE ===== -->
    <div class="tab-content" id="tab-profile">
      <div class="card">
        <div class="card-title">👤 Профиль</div>
        <div id="profileInfo"></div>
      </div>
      <div class="card">
        <div class="card-title">📊 Статистика</div>
        <div id="profileStats"></div>
      </div>
      <div class="card">
        <div class="card-title">⚙️ О приложении</div>
        <div class="row"><span class="label">Версия</span><span class="value">2.0.2</span></div>
        <div class="row"><span class="label">Платформа</span><span class="value">Argos VPN</span></div>
        <div class="row"><span class="label">Поддержка</span><span class="value">@argoossso_vpn_bot</span></div>
        <div class="row"><span class="label">Сайт</span><span class="value">argosvpn.app</span></div>
      </div>
    </div>

  </div>
</div>

<div class="toast" id="toast"></div>

<script>
const tg = window.Telegram.WebApp;
tg.ready();
tg.expand();
tg.enableClosingConfirmation();

const user = tg.initDataUnsafe.user || {};
const uid = user.id;
const uname = user.username || '';
const API = ''; // same origin

// ---- Utils ----
let qrInstance = null;
let clientData = null;

function $(id){return document.getElementById(id)}

function showToast(msg){
  const t=$('toast');t.textContent=msg;t.classList.add('show');
  clearTimeout(t._timer);t._timer=setTimeout(()=>t.classList.remove('show'),2000)
}

function showError(msg){
  $('loader').style.display='none';
  const eb=$('errorBlock');$('errorText').textContent=msg;eb.style.display='block'
}

function daysLeft(ts){return Math.max(0,Math.floor((ts-Date.now()/1000)/86400))}
function trafficGB(b){return(b/(1024**3)).toFixed(2)}
function formatDate(ts){return new Date(ts*1000).toLocaleDateString('ru-RU',{day:'numeric',month:'long',year:'numeric'})}

// ---- Tabs ----
document.querySelectorAll('.tab').forEach(tab=>{
  tab.onclick=()=>{
    document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(t=>t.classList.remove('active'));
    tab.classList.add('active');
    $(`tab-${tab.dataset.tab}`).classList.add('active');
    if(tab.dataset.tab==='config') refreshConfigTab();
    if(tab.dataset.tab==='servers') refreshServersTab();
    if(tab.dataset.tab==='profile') refreshProfileTab();
    tg.expand();
  }
});

// ---- API calls ----
async function apiFetch(path,opts={}){
  const r=await fetch(`${API}${path}`,{headers:{'Content-Type':'application/json'},...opts});
  if(!r.ok){const e=await r.json().catch(()=>({detail:r.statusText}));throw new Error(e.detail||`HTTP ${r.status}`)}
  return r.json()
}

async function loadClientData(){
  if(!uid) return null;
  try{
    const data=await apiFetch(`/vpn/client/${uid}`);
    clientData=data;
    return data
  }catch(e){
    clientData=null;
    return null
  }
}

// ---- DASHBOARD ----
async function refreshDashboard(){
  const data=await loadClientData();
  const el=$('dashStatus');
  if(!data){
    el.innerHTML=`
      <div style="text-align:center;padding:8px 0">
        <p style="font-size:40px;margin-bottom:8px">🔐</p>
        <p style="font-weight:500;margin-bottom:4px">Нет активного конфига</p>
        <p style="font-size:13px;color:var(--tg-theme-hint-color,#8e94a2)">Создайте конфиг во вкладке «Конфиг»</p>
      </div>`;
    drawEmptyChart();
    $('dashTrafficGb').textContent='0';
    return
  }
  const dl=daysLeft(data.expires_at);
  const badge=dl>7?'badge-ok':(dl>0?'badge-warn':'badge-exp');
  const badgeText=dl>0?(dl>1?'Активен':'Последний день'):'Истёк';
  el.innerHTML=`
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:12px">
      <span style="font-size:40px">🛡️</span>
      <div style="flex:1"><div style="font-size:16px;font-weight:600">Подключение активно</div>
      <span class="badge ${badge}" style="margin-top:4px">${badgeText}</span></div>
    </div>
    <div class="row"><span class="label">Ваш IP</span><span class="value">${data.ip||'—'}</span></div>
    <div class="row"><span class="label">Осталось</span><span class="value">${dl} ${dl===1?'день':'дней'}</span></div>
    <div class="row"><span class="label">Истекает</span><span class="value">${formatDate(data.expires_at)}</span></div>
    <div class="row"><span class="label">Ключ</span><span class="value" style="font-size:11px">${(data.public_key||'').slice(0,20)}…</span></div>`;
  $('dashTrafficGb').textContent=data.traffic_gb||'0';
  drawTrafficChart(data);
}

function drawEmptyChart(){
  const c=$('trafficChart');if(!c)return;const ctx=c.getContext('2d');
  const w=c.width,h=c.height;ctx.clearRect(0,0,w,h);
  ctx.fillStyle=window.Telegram?.WebApp?.colorScheme==='dark'?'rgba(255,255,255,.05)':'rgba(0,0,0,.05)';
  ctx.fillRect(0,0,w,h);
  ctx.fillStyle=window.Telegram?.WebApp?.colorScheme==='dark'?'rgba(255,255,255,.1)':'rgba(0,0,0,.1)';
  ctx.font='12px sans-serif';ctx.textAlign='center';ctx.fillText('Нет данных о трафике',w/2,h/2+4)
}

function drawTrafficChart(data){
  const c=$('trafficChart');if(!c)return;const ctx=c.getContext('2d');
  const w=c.width,h=c.height;ctx.clearRect(0,0,w,h);
  // Simulated daily traffic for last 7 days
  const totalGb=parseFloat(data.traffic_gb||0);
  const days=7;
  const daily=Array.from({length:days},(_,i)=>{
    const factor=(i+1)/days;
    return Math.max(0.01,totalGb*factor*(0.5+Math.random()*0.5))
  });
  const max=Math.max(...daily,0.1);
  const barW=(w-20)/days;
  const isDark=window.Telegram?.WebApp?.colorScheme==='dark';
  const barColor=window.Telegram?.WebApp?.colorScheme==='dark'?'#2563eb':'#3b82f6';
  ctx.clearRect(0,0,w,h);
  daily.forEach((v,i)=>{
    const barH=(v/max)*(h-16);
    const x=10+i*barW+2;
    const gradient=ctx.createLinearGradient(x,h-4,x,h-barH-4);
    gradient.addColorStop(0,barColor);
    gradient.addColorStop(1,barColor+'88');
    ctx.fillStyle=gradient;
    ctx.beginPath();
    ctx.roundRect(x,h-barH-4,barW-4,barH,4);
    ctx.fill();
  });
  ctx.fillStyle=isDark?'rgba(255,255,255,.3)':'rgba(0,0,0,.3)';
  ctx.font='9px sans-serif';ctx.textAlign='center';
  const daysShort=['Пн','Вт','Ср','Чт','Пт','Сб','Вс'];
  const today=new Date().getDay();
  daily.forEach((_,i)=>{
    const dayIdx=(today-days+i+7)%7;
    ctx.fillText(daysShort[dayIdx],10+i*barW+barW/2,h-4)
  })
}

$('dashGetConfig').onclick=()=>{document.querySelector('[data-tab="config"]').click()};
$('dashExtend').onclick=()=>{document.querySelector('[data-tab="config"]').click();showExtendModal()};

// ---- CONFIG ----
async function refreshConfigTab(){
  const data=await loadClientData();
  if(!data){
    $('configNoConfig').style.display='block';
    $('configExists').style.display='none';
    return
  }
  $('configNoConfig').style.display='none';
  $('configExists').style.display='block';

  // Config text
  try {
    const r=await fetch('/vpn/client/create',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({telegram_id:uid,username:uname})
    });
    const d=await r.json();
    if(d.config){
      $('configText').textContent=d.config;
      // QR
      if($('qrContainer')){
        $('qrContainer').innerHTML='';
        try{
          qrInstance=new QRCode($('qrContainer'),{
            text:d.config,width:240,height:240,colorDark:'#000',colorLight:'#fff',correctLevel:QRCode.CorrectLevel.M
          })
        }catch(e){
          $('qrContainer').innerHTML='<p style="font-size:12px;color:var(--tg-theme-hint-color,#8e94a2)">QR: ошибка генерации</p>'
        }
      }
    }
  }catch(e){showToast('Ошибка загрузки конфига')}

  const dl=daysLeft(data.expires_at);
  const badge=dl>1?'badge-ok':(dl===1?'badge-warn':'badge-exp');
  $('configStatusBadge').className=`badge ${badge}`;
  $('configStatusBadge').textContent=dl>0?'Активен':'Истёк';

  $('configInfo').innerHTML=`
    <div class="row"><span class="label">Ваш IP в сети</span><span class="value">${data.ip||'—'}</span></div>
    <div class="row"><span class="label">Дней осталось</span><span class="value">${dl}</span></div>
    <div class="row"><span class="label">Истекает</span><span class="value">${formatDate(data.expires_at)}</span></div>
    <div class="row"><span class="label">Трафик</span><span class="value">${data.traffic_gb||'0'} GB / 5 GB</span></div>
    <button class="btn btn-sm btn-outline" onclick="showExtendModal()" style="margin-top:8px">🔄 Продлить</button>`;
}

$('configCreateBtn').onclick=async function(){
  this.disabled=true;this.textContent='Создание...';
  try{
    await apiFetch('/vpn/client/create',{method:'POST',body:JSON.stringify({telegram_id:uid,username:uname})});
    showToast('Конфиг создан!');await refreshConfigTab()
  }catch(e){showError(e.message)}
  finally{this.disabled=false;this.textContent='Создать конфиг'}
};

$('configCopyBtn').onclick=function(){
  const text=$('configText').textContent;
  if(navigator.clipboard&&navigator.clipboard.writeText)
    navigator.clipboard.writeText(text).then(()=>showToast('Конфиг скопирован'));
  else{const ta=document.createElement('textarea');ta.value=text;
    document.body.appendChild(ta);ta.select();document.execCommand('copy');
    document.body.removeChild(ta);showToast('Конфиг скопирован')}
};

$('vlessCopyBtn').onclick=function(){
  const text=$('vlessText').textContent;
  if(navigator.clipboard&&navigator.clipboard.writeText)
    navigator.clipboard.writeText(text).then(()=>showToast('VLESS скопирован'));
  else{const ta=document.createElement('textarea');ta.value=text;
    document.body.appendChild(ta);ta.select();document.execCommand('copy');
    document.body.removeChild(ta);showToast('VLESS скопирован')}
};

$('configDownloadBtn').onclick=function(){
  const text=$('configText').textContent;
  const blob=new Blob([text],{type:'text/plain;charset=utf-8'});
  const a=document.createElement('a');a.href=URL.createObjectURL(blob);
  a.download='argos_'+(uname||uid)+'.conf';a.click();URL.revokeObjectURL(a.href);
  showToast('Файл скачан')
};

$('configDeleteBtn').onclick=async function(){
  if(!confirm('Удалить конфиг? Подключение будет разорвано.')) return;
  try{
    await apiFetch(`/vpn/client/${uid}`,{method:'DELETE'});
    showToast('Конфиг удалён');clientData=null;
    await refreshConfigTab();await refreshDashboard()
  }catch(e){showToast('Ошибка: '+e.message)}
};

// Extend
function showExtendModal(){$('extendModal').classList.add('show')}
function closeExtendModal(){$('extendModal').classList.remove('show')}
$('extendModal').onclick=function(e){if(e.target===this)closeExtendModal()};

async function extendDays(days){
  try{
    await apiFetch('/vpn/client/extend',{method:'POST',body:JSON.stringify({telegram_id:uid,days})});
    showToast(`Подписка продлена на ${days} дн.`);
    closeExtendModal();await refreshConfigTab();await refreshDashboard()
  }catch(e){showToast('Ошибка: '+e.message);closeExtendModal()}
}

// ---- SERVERS ----
async function refreshServersTab(){
  try{
    const servers=await apiFetch('/vpn/servers');
    const el=$('serverList');
    el.innerHTML=servers.map(s=>`
      <div class="server-card" data-id="${s.id}" onclick="selectServer('${s.id}')">
        <span class="server-flag">${s.flag||'🌍'}</span>
        <div class="server-info">
          <div class="server-name">${s.name}</div>
          <div class="server-loc">${s.location||''} &middot; ${s.ping||'—'} ms</div>
        </div>
        <div class="server-meta">
          <span class="${s.load<50?'pulse pulse-green':(s.load<75?'pulse pulse-yellow':'pulse pulse-red')}"></span>
          ${s.load||0}%<br>
          <span style="font-size:10px">загрузка</span>
        </div>
      </div>
    `).join('');

    $('networkStatus').innerHTML=`
      <div class="row"><span class="label">Доступно серверов</span><span class="value">${servers.length}</span></div>
      <div class="row"><span class="label">Средний ping</span><span class="value">${Math.round(servers.reduce((a,s)=>a+(s.ping||0),0)/servers.length)} ms</span></div>
      <div class="row"><span class="label">Средняя загрузка</span><span class="value">${Math.round(servers.reduce((a,s)=>a+(s.load||0),0)/servers.length)}%</span></div>`
  }catch(e){$('serverList').innerHTML=`<p style="color:var(--tg-theme-hint-color,#8e94a2);text-align:center">Ошибка загрузки</p>`}
}

function selectServer(id){
  document.querySelectorAll('.server-card').forEach(c=>c.classList.remove('selected'));
  const el=document.querySelector(`[data-id="${id}"]`);
  if(el)el.classList.add('selected');
  showToast('Сервер выбран');
  // Store preference
  try{tg?.CloudStorage?.setItem('preferred_server',id,()=>{})}catch(e){}
}

// ---- PROFILE ----
async function refreshProfileTab(){
  const data=await loadClientData();
  if(data){
    const dl=daysLeft(data.expires_at);
    $('profileInfo').innerHTML=`
      <div class="row"><span class="label">Telegram ID</span><span class="value" style="font-size:12px">${uid||'—'}</span></div>
      <div class="row"><span class="label">Username</span><span class="value">${uname||'—'}</span></div>
      <div class="row"><span class="label">VPN IP</span><span class="value">${data.ip||'—'}</span></div>
      <div class="row"><span class="label">Статус</span><span class="badge ${dl>0?'badge-ok':'badge-exp'}">${dl>0?'Активен':'Истёк'}</span></div>`;
    $('profileStats').innerHTML=`
      <div class="row"><span class="label">Всего сессий</span><span class="value">1</span></div>
      <div class="row"><span class="label">Использовано трафика</span><span class="value">${data.traffic_gb||'0'} GB</span></div>
      <div class="row"><span class="label">Лимит трафика</span><span class="value">5 GB</span></div>
      <div class="row"><span class="label">Осталось трафика</span><span class="value">${(5-parseFloat(data.traffic_gb||0)).toFixed(2)} GB</span></div>`
  }else{
    $('profileInfo').innerHTML=`<div class="empty-state"><p>Нет данных профиля</p></div>`;
    $('profileStats').innerHTML=`<div class="empty-state"><p>Создайте конфиг в разделе «Конфиг»</p></div>`
  }
}

// ---- INIT ----
(async function(){
  if(!uid){$('loader').style.display='none';showError('Запустите приложение через Telegram');return}
  try{
    await loadClientData();
    $('loader').style.display='none';
    $('app').style.display='block';
    await refreshDashboard();
    tg.expand();
    // Auto-refresh dashboard every 30s
    setInterval(refreshDashboard,30000)
  }catch(e){
    showError('Ошибка загрузки: '+e.message)
  }
})();
</script>
</body>
</html>"""


@router.get("/qr/{telegram_id}")
async def vpn_qr(telegram_id: int) -> Response:
    db = _get_db()
    wg = _get_wg()
    user = db.get_user(telegram_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    key = db.get_active_key(user["id"])
    if not key:
        raise HTTPException(status_code=404, detail="No active config")
    server_ip = os.getenv("ARGOS_VPN_SERVER_IP", os.getenv("SERVER_IP", "your-server.com"))
    config = wg.generate_client_config(key["private_key"], key["ip_address"], server_ip=server_ip)
    try:
        import qrcode
        from qrcode.image.pil import PilImage
        img = qrcode.make(config, box_size=10, border=2)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return Response(content=buf.getvalue(), media_type="image/png")
    except ImportError:
        b64 = base64.b64encode(config.encode()).decode()
        svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 256 256">
          <rect width="256" height="256" fill="#fff"/>
          <text x="128" y="120" text-anchor="middle" font-family="monospace" font-size="11" fill="#333">QR (qrcode[pil] not installed)</text>
          <text x="128" y="140" text-anchor="middle" font-family="monospace" font-size="9" fill="#666">Install: pip install qrcode[pil]</text>
        </svg>'''
        return Response(content=svg, media_type="image/svg+xml")


@router.post("/client/extend")
async def extend_client(request: ExtendRequest) -> dict[str, Any]:
    db = _get_db()
    user = db.get_user(request.telegram_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    key = db.get_active_key(user["id"])
    if not key:
        raise HTTPException(status_code=404, detail="No active config")
    new_expires = int(time.time()) + request.days * 86400
    with db._connect() as conn:
        conn.execute("UPDATE wg_keys SET expires_at=? WHERE id=?", (new_expires, key["id"]))
        conn.commit()
    return {
        "status": "extended",
        "telegram_id": request.telegram_id,
        "new_expires_at": new_expires,
        "days_added": request.days,
    }


@router.delete("/client/{telegram_id}")
async def delete_client(telegram_id: int) -> dict[str, Any]:
    db = _get_db()
    wg = _get_wg()
    user = db.get_user(telegram_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    key = db.get_active_key(user["id"])
    if not key:
        raise HTTPException(status_code=404, detail="No active config")
    db.deactivate_key(key["public_key"])
    try:
        wg.remove_peer(key["public_key"])
    except Exception:
        pass
    return {"status": "deleted", "telegram_id": telegram_id, "ip": key["ip_address"]}


@router.get("/servers")
async def list_servers() -> list[dict[str, Any]]:
    return SERVERS


@router.get("/vless/config")
async def vless_config() -> dict[str, Any]:
    return {
        "protocol": "VLESS+WebSocket+TLS",
        "address": "vpn.argosssss.win",
        "port": 443,
        "uuid": "bcdae1c0-93ab-49b1-b3a8-8465b982f888",
        "path": "/ray",
        "security": "tls",
        "sni": "vpn.argosssss.win",
        "transport": "ws",
        "link": "vless://bcdae1c0-93ab-49b1-b3a8-8465b982f888@vpn.argosssss.win:443?encryption=none&security=tls&sni=vpn.argosssss.win&type=ws&path=%2Fray&host=vpn.argosssss.win#Argos-CF-WS",
    }


@router.get("/qr_vless")
async def vless_qr() -> Response:
    import qrcode  # type: ignore
    import io as _io
    link = "vless://bcdae1c0-93ab-49b1-b3a8-8465b982f888@vpn.argosssss.win:443?encryption=none&security=tls&sni=vpn.argosssss.win&type=ws&path=%2Fray&host=vpn.argosssss.win#Argos-CF-WS"
    img = qrcode.make(link)
    buf = _io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return Response(content=buf.getvalue(), media_type="image/png")


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
