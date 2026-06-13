"""
usb_access_point.py — USB-гаджет + WiFi точка доступа + веб-морда ARGOS

Сценарий:
  1. Устройство (OPi/RPi/Android) подключается к ПК через USB
  2. USB-гаджет создаёт виртуальный Ethernet-адаптер (NCM/RNDIS/ECM)
  3. ARGOS поднимает DHCP-сервер на usb0 → ПК получает IP
  4. Дополнительно: WiFi точка доступа (hostapd) для мобильных устройств
  5. FastAPI веб-морда доступна на http://192.168.7.1:8000

Команды Telegram:
  запусти точку доступа      — USB + WiFi AP + веб-морда
  usb гаджет статус          — состояние usb0
  wifi ap статус             — состояние WiFi точки доступа
  веб морда                  — открыть / запустить веб-интерфейс
  стоп точки доступа         — остановить всё
"""

from __future__ import annotations

SKILL_DESCRIPTION = "USB-гаджет + WiFi AP + веб-морда ARGOS"

import os
import subprocess
import threading
import time
from src.argos_logger import get_logger

log = get_logger("argos.usb_ap")

# ── Конфигурация ──────────────────────────────────────────────────────────────
USB_IFACE    = os.getenv("ARGOS_USB_IFACE",    "usb0")
USB_IP       = os.getenv("ARGOS_USB_IP",       "192.168.7.1")      # IP устройства
USB_PEER     = os.getenv("ARGOS_USB_PEER",     "192.168.7.2")      # IP ПК (DHCP)
USB_SUBNET   = os.getenv("ARGOS_USB_SUBNET",   "255.255.255.0")

WIFI_IFACE   = os.getenv("ARGOS_WIFI_IFACE",   "wlan0")
WIFI_SSID    = os.getenv("ARGOS_WIFI_SSID",    "ARGOS_AP")
WIFI_PASS    = os.getenv("ARGOS_WIFI_PASS",    "argos1234")
WIFI_CHANNEL = os.getenv("ARGOS_WIFI_CHANNEL", "6")

WEB_PORT     = int(os.getenv("ARGOS_WEB_PORT", "8000"))
WEB_HOST     = "0.0.0.0"

IS_LINUX  = os.path.exists("/proc/version")
IS_ANDROID = os.path.exists("/system/build.prop")

TRIGGERS = [
    "точка доступа", "usb ap", "usb gadget", "usb гаджет",
    "wifi ap", "wifi точка", "hostapd", "ap старт", "ap стоп",
    "веб морда", "веб-морда", "webui", "web interface",
    "запусти ap", "запусти точку", "остановить ap",
]


def _run(cmd: str, sudo: bool = True) -> tuple[int, str]:
    """Выполняет shell-команду, возвращает (returncode, output)."""
    prefix = "sudo " if (sudo and os.geteuid() != 0) else ""
    try:
        r = subprocess.run(
            prefix + cmd, shell=True,
            capture_output=True, text=True, timeout=15
        )
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as e:
        return -1, str(e)


class USBGadgetAP:
    """
    Управление USB-гаджетом (NCM/RNDIS) + WiFi AP + веб-мордой.
    """

    def __init__(self, core=None):
        self.core       = core
        self._usb_up    = False
        self._wifi_up   = False
        self._web_up    = False
        self._web_thread: threading.Thread | None = None

    # ── USB-гаджет ────────────────────────────────────────────────────────────

    def setup_usb_gadget(self) -> str:
        """
        Настраивает USB-гаджет через ConfigFS (Linux/OPi/RPi).
        Создаёт виртуальный Ethernet-интерфейс usb0 с фиксированным IP.
        """
        if not IS_LINUX:
            return "❌ USB-гаджет: только Linux (OPi/RPi/Android)"

        lines = ["🔌 USB-гаджет настройка:"]

        # 1. Проверяем что интерфейс usb0 уже есть (ConfigFS уже настроен ядром)
        rc, out = _run(f"ip link show {USB_IFACE}", sudo=False)
        if rc == 0:
            lines.append(f"  ✅ Интерфейс {USB_IFACE} обнаружен")
        else:
            # Пробуем загрузить g_ether / usb_f_ncm
            for mod in ("g_ether", "usb_f_ncm", "libcomposite"):
                _run(f"modprobe {mod}")
            time.sleep(1)
            rc, out = _run(f"ip link show {USB_IFACE}", sudo=False)
            if rc != 0:
                lines.append(f"  ⚠️  {USB_IFACE} не найден — возможно нужен ConfigFS или Android USB tethering")
                lines.append("  📌 На OPi/RPi: включи USB Gadget в Device Tree Overlays")
                lines.append("  📌 На Android: Настройки → Сеть → USB-модем → включить")

        # 2. Поднимаем интерфейс и назначаем IP
        _run(f"ip link set {USB_IFACE} up")
        rc, out = _run(f"ip addr add {USB_IP}/{USB_SUBNET} dev {USB_IFACE} 2>/dev/null || true")
        lines.append(f"  📍 IP назначен: {USB_IP} на {USB_IFACE}")

        # 3. Включаем IP-форвардинг (для NAT через WiFi)
        _run("sysctl -w net.ipv4.ip_forward=1")

        # 4. Простой DHCP через dnsmasq (если установлен)
        rc_dnsmasq, _ = _run("which dnsmasq", sudo=False)
        if rc_dnsmasq == 0:
            # Убиваем предыдущий dnsmasq на usb0
            _run(f"pkill -f 'dnsmasq.*{USB_IFACE}'")
            rc, out = _run(
                f"dnsmasq --interface={USB_IFACE} "
                f"--dhcp-range={USB_PEER},{USB_PEER},12h "
                f"--dhcp-option=3,{USB_IP} "   # шлюз
                f"--dhcp-option=6,{USB_IP} "   # DNS
                f"--no-resolv --no-daemon &"
            )
            lines.append(f"  ✅ DHCP (dnsmasq): ПК получит IP {USB_PEER}")
        else:
            lines.append(f"  ⚠️  dnsmasq не найден — установи: apt install dnsmasq")
            lines.append(f"  📌 Вручную задай на ПК: IP={USB_PEER} маска=255.255.255.0 шлюз={USB_IP}")

        self._usb_up = True
        lines.append(f"\n  🌐 Подключись к ПК: http://{USB_IP}:{WEB_PORT}")
        return "\n".join(lines)

    # ── WiFi точка доступа ────────────────────────────────────────────────────

    def setup_wifi_ap(self) -> str:
        """Поднимает WiFi AP через hostapd + dnsmasq."""
        if not IS_LINUX:
            return "❌ WiFi AP: только Linux"

        lines = [f"📡 WiFi точка доступа [{WIFI_SSID}]:"]

        # Проверяем hostapd
        rc, _ = _run("which hostapd", sudo=False)
        if rc != 0:
            return "❌ hostapd не установлен: apt install hostapd"

        # Генерируем конфиг hostapd
        conf_path = "/tmp/argos_hostapd.conf"
        conf = f"""interface={WIFI_IFACE}
driver=nl80211
ssid={WIFI_SSID}
hw_mode=g
channel={WIFI_CHANNEL}
wmm_enabled=0
macaddr_acl=0
auth_algs=1
ignore_broadcast_ssid=0
wpa=2
wpa_passphrase={WIFI_PASS}
wpa_key_mgmt=WPA-PSK
wpa_pairwise=TKIP
rsn_pairwise=CCMP
"""
        try:
            with open(conf_path, "w") as f:
                f.write(conf)
            lines.append(f"  ✅ Конфиг: {conf_path}")
        except Exception as e:
            return f"❌ Не удалось записать конфиг: {e}"

        # Настраиваем wlan0 IP
        _run(f"ip link set {WIFI_IFACE} up")
        _run(f"ip addr add 192.168.8.1/24 dev {WIFI_IFACE} 2>/dev/null || true")

        # Запускаем hostapd в фоне
        _run("pkill hostapd")
        rc, out = _run(f"hostapd -B {conf_path}")
        if rc != 0:
            lines.append(f"  ⚠️  hostapd: {out[:100]}")
        else:
            lines.append(f"  ✅ hostapd запущен: SSID={WIFI_SSID}, пароль={WIFI_PASS}")

        # DHCP для WiFi клиентов
        rc_dnsmasq, _ = _run("which dnsmasq", sudo=False)
        if rc_dnsmasq == 0:
            _run(f"pkill -f 'dnsmasq.*{WIFI_IFACE}'")
            _run(
                f"dnsmasq --interface={WIFI_IFACE} "
                f"--dhcp-range=192.168.8.2,192.168.8.20,12h "
                f"--dhcp-option=3,192.168.8.1 "
                f"--no-resolv --no-daemon &"
            )
            lines.append(f"  ✅ DHCP: 192.168.8.2–20")

        self._wifi_up = True
        lines.append(f"\n  📱 Телефон: подключись к {WIFI_SSID} → http://192.168.8.1:{WEB_PORT}")
        return "\n".join(lines)

    # ── Веб-морда ─────────────────────────────────────────────────────────────

    def start_web_ui(self) -> str:
        """Запускает FastAPI веб-морду ARGOS."""
        if self._web_up:
            return f"🌐 Веб-морда уже запущена: http://{USB_IP}:{WEB_PORT}"

        def _run_web():
            try:
                # Пробуем FastAPIDashboard (полный дашборд)
                try:
                    from src.interface.fastapi_dashboard import FastAPIDashboard
                    dash = FastAPIDashboard(self.core, port=WEB_PORT, host=WEB_HOST)
                    dash.start()
                    return
                except Exception:
                    pass

                # Fallback: минимальный FastAPI
                try:
                    from fastapi import FastAPI
                    from fastapi.responses import HTMLResponse, JSONResponse
                    import uvicorn

                    app = FastAPI(title="ARGOS")

                    @app.get("/", response_class=HTMLResponse)
                    async def index():
                        return _minimal_ui()

                    @app.post("/api/command")
                    async def command(req: dict):
                        if self.core:
                            result = self.core.process(req.get("text", ""))
                            return {"answer": result.get("answer", "")}
                        return {"answer": "core недоступен"}

                    @app.get("/api/status")
                    async def status():
                        try:
                            import psutil
                            return {
                                "cpu": psutil.cpu_percent(0.1),
                                "ram": psutil.virtual_memory().percent,
                                "disk": psutil.disk_usage("/").percent,
                            }
                        except Exception:
                            return {}

                    uvicorn.run(app, host=WEB_HOST, port=WEB_PORT, log_level="error")
                except ImportError:
                    # Fallback: встроенный http.server
                    import http.server, socketserver, json as _json

                    class Handler(http.server.BaseHTTPRequestHandler):
                        def do_GET(self):
                            self.send_response(200)
                            self.send_header("Content-type", "text/html; charset=utf-8")
                            self.end_headers()
                            self.wfile.write(_minimal_ui().encode())
                        def log_message(self, *a): pass

                    with socketserver.TCPServer((WEB_HOST, WEB_PORT), Handler) as httpd:
                        httpd.serve_forever()
            except Exception as e:
                log.error("Веб-морда: %s", e)

        self._web_thread = threading.Thread(target=_run_web, daemon=True, name="argos-webui")
        self._web_thread.start()
        self._web_up = True
        time.sleep(0.5)
        return f"🌐 Веб-морда запущена:\n  USB:  http://{USB_IP}:{WEB_PORT}\n  WiFi: http://192.168.8.1:{WEB_PORT}\n  Локально: http://localhost:{WEB_PORT}"

    def stop(self) -> str:
        """Останавливает всё."""
        lines = ["🛑 Остановка:"]
        if self._usb_up:
            _run(f"ip addr flush dev {USB_IFACE}")
            _run(f"pkill -f 'dnsmasq.*{USB_IFACE}'")
            lines.append(f"  ✅ USB {USB_IFACE} сброшен")
            self._usb_up = False
        if self._wifi_up:
            _run("pkill hostapd")
            _run(f"pkill -f 'dnsmasq.*{WIFI_IFACE}'")
            lines.append("  ✅ WiFi AP остановлен")
            self._wifi_up = False
        return "\n".join(lines)

    def status(self) -> str:
        lines = ["📡 USB Access Point:"]
        # USB интерфейс
        rc, out = _run(f"ip addr show {USB_IFACE} 2>/dev/null", sudo=False)
        if rc == 0 and USB_IP in out:
            lines.append(f"  ✅ USB ({USB_IFACE}): {USB_IP} — активен")
        else:
            lines.append(f"  ❌ USB ({USB_IFACE}): не активен")

        # WiFi AP
        rc, out = _run("pgrep -a hostapd", sudo=False)
        if rc == 0:
            lines.append(f"  ✅ WiFi AP ({WIFI_SSID}): работает")
        else:
            lines.append(f"  ❌ WiFi AP: не запущен")

        # Веб-морда
        if self._web_up:
            lines.append(f"  ✅ Веб-морда: http://{USB_IP}:{WEB_PORT}")
        else:
            lines.append(f"  ❌ Веб-морда: не запущена")

        lines.append(f"\nКоманды: 'запусти точку доступа' | 'стоп точки доступа' | 'веб морда'")
        return "\n".join(lines)

    def execute(self, text: str = "") -> str:
        """Точка входа SkillLoader."""
        t = (text or "").strip()
        result = handle(t or "статус", self.core)
        return result if result is not None else self.status()


def _minimal_ui() -> str:
    """Минимальный HTML-интерфейс ARGOS."""
    return """<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ARGOS Control</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { background: #0d1117; color: #c9d1d9; font: 15px/1.5 monospace; }
    header { background: #161b22; padding: 12px 20px; border-bottom: 1px solid #30363d;
             display: flex; justify-content: space-between; align-items: center; }
    header h1 { color: #58a6ff; font-size: 20px; }
    #status { font-size: 12px; color: #8b949e; }
    main { padding: 20px; max-width: 900px; margin: 0 auto; }
    .cmd-box { display: flex; gap: 8px; margin: 20px 0; }
    #cmd { flex: 1; background: #161b22; border: 1px solid #30363d; color: #c9d1d9;
           padding: 10px 14px; border-radius: 6px; font: inherit; outline: none; }
    #cmd:focus { border-color: #58a6ff; }
    button { background: #238636; color: #fff; border: none; padding: 10px 18px;
             border-radius: 6px; cursor: pointer; font: inherit; }
    button:hover { background: #2ea043; }
    #output { background: #161b22; border: 1px solid #30363d; border-radius: 6px;
              padding: 16px; min-height: 200px; white-space: pre-wrap; font-size: 13px; }
    .stats { display: flex; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
    .stat { background: #161b22; border: 1px solid #30363d; border-radius: 6px;
            padding: 10px 16px; flex: 1; min-width: 100px; text-align: center; }
    .stat .val { font-size: 24px; font-weight: bold; color: #58a6ff; }
    .stat .lbl { font-size: 11px; color: #8b949e; }
  </style>
</head>
<body>
<header>
  <h1>🤖 ARGOS</h1>
  <div id="status">подключение...</div>
</header>
<main>
  <div class="stats">
    <div class="stat"><div class="val" id="cpu">—</div><div class="lbl">CPU %</div></div>
    <div class="stat"><div class="val" id="ram">—</div><div class="lbl">RAM %</div></div>
    <div class="stat"><div class="val" id="disk">—</div><div class="lbl">DISK %</div></div>
  </div>
  <div class="cmd-box">
    <input id="cmd" placeholder="Введи команду... (напр: статус системы, крипто, skill)" autofocus>
    <button onclick="send()">Отправить</button>
  </div>
  <div id="output">Готов к работе.</div>
</main>
<script>
async function send() {
  const text = document.getElementById('cmd').value.trim();
  if (!text) return;
  const out = document.getElementById('output');
  out.textContent = '⏳ Обрабатываю...';
  try {
    const r = await fetch('/api/command', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({text})
    });
    const d = await r.json();
    out.textContent = d.answer || '(нет ответа)';
  } catch(e) { out.textContent = '❌ ' + e; }
}
document.getElementById('cmd').addEventListener('keydown', e => e.key === 'Enter' && send());

async function updateStats() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    if (d.cpu !== undefined) {
      document.getElementById('cpu').textContent  = d.cpu.toFixed(0);
      document.getElementById('ram').textContent  = d.ram.toFixed(0);
      document.getElementById('disk').textContent = d.disk.toFixed(0);
      document.getElementById('status').textContent = '✅ подключено';
    }
  } catch(e) {
    document.getElementById('status').textContent = '❌ нет связи';
  }
}
setInterval(updateStats, 3000);
updateStats();
</script>
</body>
</html>"""


# ── Singleton ─────────────────────────────────────────────────────────────────
_instance: USBGadgetAP | None = None


def get_instance(core=None) -> USBGadgetAP:
    global _instance
    if _instance is None:
        _instance = USBGadgetAP(core)
    elif core and _instance.core is None:
        _instance.core = core
    return _instance


def handle(text: str, core=None) -> str | None:
    t = text.lower().strip()
    ap = get_instance(core)

    if any(k in t for k in ["запусти точку доступа", "usb ap старт", "точка доступа старт",
                              "запусти ap", "включи точку доступа"]):
        lines = []
        lines.append(ap.setup_usb_gadget())
        lines.append("")
        lines.append(ap.setup_wifi_ap())
        lines.append("")
        lines.append(ap.start_web_ui())
        return "\n".join(lines)

    if any(k in t for k in ["usb гаджет", "usb gadget", "usb статус", "настрой usb"]):
        return ap.setup_usb_gadget()

    if any(k in t for k in ["wifi ap", "wifi точка", "точка доступа wifi",
                              "hostapd", "wifi ap статус"]):
        if "статус" in t:
            return ap.status()
        return ap.setup_wifi_ap()

    if any(k in t for k in ["веб морда", "веб-морда", "webui", "web ui",
                              "запусти веб", "web interface", "интерфейс argos"]):
        return ap.start_web_ui()

    if any(k in t for k in ["стоп точки доступа", "остановить ap", "выключи точку доступа",
                              "ap стоп"]):
        return ap.stop()

    if any(k in t for k in ["точка доступа статус", "ap статус", "usb ap статус",
                              "статус точки доступа"]):
        return ap.status()

    return None


def setup(core=None):
    pass
