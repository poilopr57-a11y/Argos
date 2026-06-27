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
<button class="tab active" data-tab="chat" style="border:none;background:none;color:var(--tg-theme-hint-color,#8e94a2);padding:10px 14px;font-size:14px;border-bottom:2px solid transparent">Чат</button>
<button class="tab" data-tab="status" style="border:none;background:none;color:var(--tg-theme-hint-color,#8e94a2);padding:10px 14px;font-size:14px;border-bottom:2px solid transparent">Статус</button>
<button class="tab" data-tab="skills" style="border:none;background:none;color:var(--tg-theme-hint-color,#8e94a2);padding:10px 14px;font-size:14px;border-bottom:2px solid transparent">Навыки</button>
<button class="tab" data-tab="actions" style="border:none;background:none;color:var(--tg-theme-hint-color,#8e94a2);padding:10px 14px;font-size:14px;border-bottom:2px solid transparent">Ещё</button>
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
var chat=$('chat'),inp=$('inp'),btn=$('send');

function addMsg(t,u){
  var d=document.createElement('div');
  d.className='msg '+(u?'msg-u':'msg-b');
  d.textContent=t;chat.appendChild(d);chat.scrollTop=chat.scrollHeight;
}

function api(text,cb){
  var x=new XMLHttpRequest();
  x.open('POST','/argos/api',true);
  x.setRequestHeader('Content-Type','application/json');
  x.timeout=20000;
  x.onload=function(){try{cb(JSON.parse(x.responseText).result||'Ok')}catch(e){cb('Error')}};
  x.onerror=function(){cb('Network error')};
  x.ontimeout=function(){cb('Timeout');x.abort()};
  x.send(JSON.stringify({method:'command',params:{text:text}}));
}

function doSend(){
  var t=inp.value.trim();if(!t)return;
  addMsg(t,true);inp.value='';btn.disabled=true;
  api(t,function(r){addMsg(r,false);btn.disabled=false});
}
btn.onclick=doSend;inp.onkeydown=function(e){if(e.key=='Enter')doSend()};

// Tabs - exact VPN WebApp pattern
document.querySelectorAll('.tab').forEach(function(tab){
  tab.onclick=function(){
    document.querySelectorAll('.tab').forEach(function(t){t.style.borderBottomColor='transparent';t.style.color='var(--tg-theme-hint-color,#8e94a2)'});
    document.querySelectorAll('.tab-content').forEach(function(t){t.style.display='none'});
    tab.style.borderBottomColor='var(--tg-theme-button-color,#2563eb)';tab.style.color='var(--tg-theme-text-color,#e8ecf4)';
    $('tab-'+tab.dataset.tab).style.display='block';
    if(tab.dataset.tab=='status')loadStatus();
    if(tab.dataset.tab=='skills')loadSkills();
    if(tab.dataset.tab=='actions')loadActions();
    W.expand();
  }
});

// Mark first tab
var firstTab=document.querySelector('.tab');
if(firstTab){firstTab.style.borderBottomColor='var(--tg-theme-button-color,#2563eb)';firstTab.style.color='var(--tg-theme-text-color,#e8ecf4)'}

function setOut(id,text){
  $('statusOut').textContent='...'; $('providersOut').textContent='...'; $('gpuOut').textContent='...';
  api('mcp debug',function(r){$('statusOut').textContent=r});
  api('providers',function(r){$('providersOut').textContent=r});
  api('gpu status',function(r){$('gpuOut').textContent=r});
}
function loadStatus(){setOut('statusOut','');setOut('providersOut','');setOut('gpuOut','')}
function loadSkills(){
  $('skillsOut').textContent='Loading...';
  api('список навыков',function(r){$('skillsOut').textContent=r});
}
function loadActions(){
  var cmds=[
    ['Система','статус системы'],['GPU','gpu status'],['P2P','p2p status'],
    ['Память','mempalace status'],['Obsidian','obsidian status'],['Telegram','telegram status'],
    ['ИИ','providers'],['VPN','vpn status'],['Помощь','помощь']
  ];
  var h='';for(var i=0;i<cmds.length;i++){h+='<div class="quick-btn" onclick="quickCmd(\''+cmds[i][1]+'\')">'+cmds[i][0]+'</div>'}
  $('actionsOut').innerHTML=h;
}
function quickCmd(cmd){
  var tabs=document.querySelectorAll('.tab');
  tabs[0].click();inp.value=cmd;doSend();
}
</script>
</body>
</html>"""