"""
esp32_usb_bridge.py — ARGOS ↔ ESP32/RP2350 USB мост реального времени

Функции:
  • Автоопределение COM-порта с ARGOS-устройством (ESP32 и RP2350)
  • Отправка статуса ПК на устройство каждые 2 сек (JSON по Serial)
  • Приём команд от ESP32/RP2350 (touch-кнопки, Web UI)
  • Ретрансляция ответов ARGOS обратно на устройство
  • OTA прошивка через точку доступа ESP32
  • Flash других ESP32/ESP8266 через AP устройства (esptool)
  • MicroPython REPL / mpremote для RP2350-GEEK
"""

SKILL_DESCRIPTION = "USB-мост для ESP32: прошивка и serial-команды"

import threading
import time
import json
import os
import platform
import subprocess
import tempfile
import requests
from pathlib import Path
from src.argos_logger import get_logger

try:
    import serial
    import serial.tools.list_ports
    _SERIAL_OK = True
except ImportError:
    _SERIAL_OK = False

try:
    import psutil
    _PSUTIL_OK = True
except ImportError:
    _PSUTIL_OK = False

log = get_logger("argos.esp32_bridge")

# ── Константы ──────────────────────────────────────────────────────────────
BAUD_RATE       = 921600
BAUD_RATE_RP    = 115200    # RP2350 USB CDC native
STATUS_INTERVAL = 2.0       # сек между отправкой статуса
ARGOS_DEVICE    = "ARGOS-ESP32-2432S024"
DEFAULT_AP_IP   = "192.168.4.1"


def _disabled_ports() -> set[str]:
    raw = os.getenv("ARGOS_ESP32_DISABLED_PORTS", "")
    return {p.strip().upper() for p in raw.split(",") if p.strip()}

# VID/PID таблица: устройство → тип
_DEVICE_VID_MAP = {
    # ESP32 / CH340 / CP210x / FTDI
    (0x303A, 0x1001): "esp32",   # Espressif USB JTAG/Serial CDC (ESP32-S3)
    (0x1A86, 0x7523): "esp32",   # CH340
    (0x1A86, 0x55D4): "esp32",   # CH9102 (ESP32-S3, C3)
    (0x10C4, 0xEA60): "esp32",   # CP2102
    (0x0403, 0x6001): "esp32",   # FTDI FT232
    (0x0403, 0x6015): "esp32",   # FTDI FT231X
    (0x239A, 0x80CB): "esp32",   # Adafruit ESP32-S2
    # RP2350 / RP2040 (Raspberry Pi / Waveshare)
    (0x2E8A, 0x0003): "rp2350",  # RP2350 BOOTSEL mode
    (0x2E8A, 0x000B): "rp2350",  # RP2350 / Waveshare RP2350-GEEK CDC
    (0x2E8A, 0x0005): "rp2040",  # RP2040 MicroPython CDC
    (0x2E8A, 0x000A): "rp2040",  # RP2040 другой
    # STM32H503 — PB_MCU01_H503A / TQ-H503A
    (0x0483, 0x5740): "stm32h503",  # STM32 USB VCP (все серии H5/F4/etc)
    (0x0483, 0x5750): "stm32h503",  # STM32H5 альтернативный PID
}

TRIGGERS = [
    "esp32", "esp32-usb", "esp bridge", "esp мост", "esp32 мост",
    "подключи esp", "esp32 старт", "esp32 стоп", "esp32 статус",
    "порты usb", "com порты", "esp веб", "esp web",
    "прошей esp", "flash esp", "flash esp32", "ota esp",
    "прошить esp", "обнови esp",
]


class ESP32UsbBridge:
    """Двунаправленный USB-мост ARGOS ↔ ESP32-2432S024 / RP2350-GEEK."""

    def __init__(self, core=None, port: str = None):
        env_port = (os.getenv("ARGOS_ESP32_PORT") or os.getenv("XIAOZHI_ESP32_PORT") or "").strip()
        self.core       = core
        self.port       = port or env_port or None
        self._ser       = None
        self._running   = False
        self._lock      = threading.Lock()
        self._ap_ip     = DEFAULT_AP_IP
        self._ap_ssid   = "ARGOS_AP"
        self._thread_rx = None
        self._thread_tx = None
        self._device_type = "esp32"   # "esp32" | "rp2350" | "rp2040"
        self._last_io_error = ""

    # ── Управление ──────────────────────────────────────────────────────
    def start(self) -> str:
        if not _SERIAL_OK:
            return "❌ pyserial не установлен: pip install pyserial --break-system-packages"
        if self._ser and self._ser.is_open and self._running:
            return self.status()
        port, dev_type = self.port, self._device_type
        if not port:
            port, dev_type = self._autodetect()
        if not port:
            return "❌ ARGOS-устройство не найдено. Подключи ESP32 или RP2350 по USB."
        if port.upper() in _disabled_ports():
            return f"⛔ USB мост не трогает {port}: порт зарезервирован для Xiaozhi Wi-Fi/OTA."
        try:
            baud = BAUD_RATE_RP if dev_type in ("rp2350", "rp2040", "stm32h503") else BAUD_RATE
            self._ser = serial.Serial(port, baud, timeout=1, write_timeout=1)
            self._device_type = dev_type
            self._running = True
            self._last_io_error = ""
            self._thread_rx = threading.Thread(target=self._rx_loop,  daemon=True, name="argos-rx")
            self._thread_tx = threading.Thread(target=self._tx_loop,  daemon=True, name="argos-tx")
            self._thread_rx.start()
            self._thread_tx.start()
            log.info("ARGOS USB мост запущен на %s (%s)", port, dev_type)
            return f"✅ ARGOS мост запущен | {port} | {dev_type.upper()} | {baud} baud"
        except Exception as e:
            return f"❌ Ошибка подключения к {port}: {e}"

    def stop(self) -> str:
        self._running = False
        if self._ser and self._ser.is_open:
            try: self._ser.close()
            except: pass
        return "⛔ ARGOS USB мост остановлен."

    def status(self) -> str:
        if not self._ser or not self._ser.is_open:
            return "📴 ARGOS устройство не подключено."
        dev = self._device_type.upper()
        if dev == "ESP32":
            return (f"✅ ESP32 мост активен | порт: {self._ser.port} | "
                    f"AP: {self._ap_ssid} ({self._ap_ip})")
        return f"✅ {dev} мост активен | порт: {self._ser.port}"

    def execute(self, text: str = "") -> str:
        """Точка входа SkillLoader."""
        t = (text or "").strip()
        if not t or t.lower() in ("статус", "status"):
            return self.status()
        result = handle(t)
        if result is not None:
            return result
        return self.status()

    # ── Авто-определение порта ───────────────────────────────────────────
    def _autodetect(self) -> tuple[str | None, str]:
        """Ищет первый ARGOS-совместимый порт. Возвращает (port, device_type)."""
        if not _SERIAL_OK:
            return None, "esp32"
        for p in serial.tools.list_ports.comports():
            if (p.device or "").upper() in _disabled_ports():
                continue
            key = (p.vid, p.pid)
            if key in _DEVICE_VID_MAP:
                dev_type = _DEVICE_VID_MAP[key]
                log.info("Найден %s порт: %s %s", dev_type.upper(), p.device, p.description)
                return p.device, dev_type
        # Fallback: CH340/CP210x/FTDI без точного PID
        for p in serial.tools.list_ports.comports():
            if (p.device or "").upper() in _disabled_ports():
                continue
            vid = p.vid or 0
            if vid in {0x1A86, 0x10C4, 0x0403, 0x067B, 0x239A}:
                log.info("Найден ESP32 порт (fallback): %s", p.device)
                return p.device, "esp32"
            if vid == 0x2E8A:
                log.info("Найден RP порт (fallback): %s", p.device)
                return p.device, "rp2350"
            if vid == 0x0483:
                log.info("Найден STM32 порт (fallback): %s", p.device)
                return p.device, "stm32h503"
        ports = list(serial.tools.list_ports.comports())
        if ports and os.getenv("ARGOS_SERIAL_ALLOW_UNKNOWN", "0").lower() in {"1", "true", "yes", "on"}:
            for p in ports:
                if (p.device or "").upper() in _disabled_ports():
                    continue
                if (p.device or "").upper() != "COM1":
                    return p.device, "esp32"
        return None, "esp32"

    def _handle_serial_io_error(self, where: str, exc: Exception) -> bool:
        winerror = getattr(exc, "winerror", None)
        errno = getattr(exc, "errno", None)
        is_access_error = isinstance(exc, PermissionError) or winerror == 5 or errno in {5, 13}
        if not is_access_error:
            log.error("%s ошибка: %s", where, exc)
            return False

        port = getattr(self._ser, "port", self.port or "?")
        msg = f"{where} access denied on {port}: {exc}"
        if msg != self._last_io_error:
            log.error("%s. Останавливаю ESP32 USB мост, чтобы не спамить лог.", msg)
            self._last_io_error = msg
        self._running = False
        try:
            if self._ser and self._ser.is_open:
                self._ser.close()
        except Exception:
            pass
        return True

    # ── Приём данных от ESP32 ────────────────────────────────────────────
    def _rx_loop(self):
        buf = ""
        while self._running:
            try:
                if self._ser and self._ser.in_waiting:
                    raw = self._ser.readline().decode("utf-8", errors="ignore").strip()
                    if raw.startswith("{"):
                        self._handle_esp_json(raw)
                else:
                    time.sleep(0.05)
            except Exception as e:
                if self._handle_serial_io_error("RX", e):
                    break
                time.sleep(1)

    def _register_in_iot_hub(self, fw: str = "?"):
        """Регистрирует ESP32/RP2350 в IoT Hub ARGOS как устройство."""
        if not self.core:
            return
        try:
            # Через IoTBridge (реестр устройств)
            if hasattr(self.core, "iot_bridge") and self.core.iot_bridge:
                from src.connectivity.iot_bridge import IoTDevice
                dt = self._device_type
                if dt in ("rp2350", "rp2040"):
                    dev_id  = "rp2350_geek"
                    label   = "RP2350-GEEK"
                    display = "ST7789 135x240"
                elif dt == "stm32h503":
                    dev_id  = "pb_mcu01_h503a"
                    label   = "PB_MCU01_H503A (STM32H503)"
                    display = "none"
                else:
                    dev_id  = "esp32_2432s024"
                    label   = "ESP32-2432S024 Display"
                    display = "ILI9341 320x240"
                dev = self.core.iot_bridge.registry.get(dev_id)
                if not dev:
                    dev = IoTDevice(dev_id, "gateway", "usb_serial",
                                    self._ser.port if self._ser else "USB", label)
                    self.core.iot_bridge.registry.register(dev)
                dev.update("fw",      fw)
                dev.update("display", display)
                if dt == "esp32":
                    dev.update("ap_ip",   self._ap_ip)
                    dev.update("ap_ssid", self._ap_ssid)
                log.info("%s зарегистрирован в IoT Hub реестре", label)

            # Через ArgosIoTHub (телеметрия)
            if hasattr(self.core, "iot_hub") and self.core.iot_hub:
                # Уведомляем хаб о новом Gateway устройстве
                try:
                    self.core.iot_hub._on_device_update(
                        "esp32_2432s024",
                        {"fw": fw, "ap_ip": self._ap_ip, "ap_ssid": self._ap_ssid, "type": "gateway"}
                    )
                except Exception:
                    pass
        except Exception as e:
            log.debug("IoT Hub регистрация ESP32: %s", e)

    def _push_telemetry_to_iot_hub(self, cpu, ram, disk_free):
        """Обновляет телеметрию ESP32-устройства в IoT Hub."""
        if not self.core:
            return
        try:
            if hasattr(self.core, "iot_bridge") and self.core.iot_bridge:
                dev = self.core.iot_bridge.registry.get("esp32_2432s024")
                if dev:
                    dev.update("pc_cpu",  cpu)
                    dev.update("pc_ram",  ram)
                    dev.update("pc_disk", disk_free)
        except Exception:
            pass

    def _handle_esp_json(self, raw: str):
        try:
            msg = json.loads(raw)
        except Exception:
            return
        t = msg.get("type", "")

        if t == "hello":
            self._ap_ip   = msg.get("ap_ip",   DEFAULT_AP_IP)
            self._ap_ssid = msg.get("ap_ssid", "ARGOS_AP")
            device_id_hint = msg.get("device", "")
            fw = msg.get("fw", "?")
            # Определяем тип устройства по полю device
            if "rp2350" in device_id_hint.lower() or "rp2040" in device_id_hint.lower():
                self._device_type = "rp2350"
            log.info("%s приветствие: device=%s fw=%s",
                     self._device_type.upper(), device_id_hint, fw)
            self._register_in_iot_hub(fw=fw)

        elif t == "telemetry":
            # RP2350-GEEK телеметрия: temp, uptime
            chip_temp = msg.get("temp", 0)
            uptime    = msg.get("uptime", 0)
            log.debug("RP2350 telemetry: temp=%.1f uptime=%d", chip_temp, uptime)
            try:
                if self.core and hasattr(self.core, "iot_bridge") and self.core.iot_bridge:
                    dev = self.core.iot_bridge.registry.get(
                        "rp2350_geek" if self._device_type != "esp32" else "esp32_2432s024"
                    )
                    if dev:
                        dev.update("chip_temp", chip_temp)
                        dev.update("uptime",    uptime)
            except Exception:
                pass

        elif t == "user_cmd":
            # Команда от touch/web — отправляем в ARGOS
            cmd = msg.get("cmd", "")
            if cmd and self.core:
                log.info("ESP32 команда: %s", cmd)
                try:
                    from src.admin import ArgosAdmin
                    result = self.core.process_logic(cmd, ArgosAdmin(), None)
                    answer = result.get("answer", str(result)) if isinstance(result, dict) else str(result)
                    self._send({"type": "reply", "text": answer[:200]})
                except Exception as e:
                    self._send({"type": "reply", "text": f"Ошибка: {e}"})

        elif t == "pong":
            log.debug("Pong от ESP32")

        elif t == "cmd_result":
            log.info("Результат команды ESP32: %s", msg)

    # ── Отправка статуса ПК на ESP32 ──────────────────────────────────
    def _tx_loop(self):
        while self._running:
            self._send_status()
            time.sleep(STATUS_INTERVAL)

    def _send_status(self):
        data = {"type": "status"}
        cpu_val = ram_val = 0
        disk_val = "--"
        if _PSUTIL_OK:
            try:
                cpu_val  = round(psutil.cpu_percent(interval=0.5), 1)
                vm       = psutil.virtual_memory()
                ram_val  = round(vm.percent, 1)
                du       = psutil.disk_usage("/")
                disk_val = f"{du.free // (1024**3)}GB"
            except Exception:
                pass
        data["cpu"]  = cpu_val
        data["ram"]  = ram_val
        data["disk"] = disk_val
        data["os"]   = platform.system()
        self._send(data)
        # Зеркалим телеметрию в IoT Hub
        self._push_telemetry_to_iot_hub(cpu_val, ram_val, disk_val)

    def _send(self, obj: dict):
        if not self._ser or not self._ser.is_open:
            return
        try:
            line = json.dumps(obj, ensure_ascii=False) + "\n"
            with self._lock:
                self._ser.write(line.encode("utf-8"))
        except Exception as e:
            self._handle_serial_io_error("TX", e)

    # ── OTA: обновление прошивки ESP32 через Wi-Fi AP ──────────────────
    def ota_update(self, firmware_path: str) -> str:
        """
        Загружает .bin файл на ESP32 через HTTP POST /ota/upload
        ESP32 должен быть подключён к AP (192.168.4.x) или AP ARGOS_AP
        """
        fw = Path(firmware_path)
        if not fw.exists():
            return f"❌ Файл не найден: {firmware_path}"
        url = f"http://{self._ap_ip}/ota/upload"
        log.info("OTA: отправка %s → %s", fw.name, url)
        try:
            with open(fw, "rb") as f:
                resp = requests.post(
                    url,
                    files={"firmware": (fw.name, f, "application/octet-stream")},
                    timeout=60
                )
            if resp.ok:
                return f"✅ OTA успешно! ESP32 перезагрузится. ({fw.name}, {fw.stat().st_size // 1024} KB)"
            else:
                return f"❌ OTA ошибка HTTP {resp.status_code}: {resp.text[:100]}"
        except requests.exceptions.ConnectionError:
            return (f"❌ Не удалось подключиться к {url}\n"
                    f"Подключись к Wi-Fi {self._ap_ssid} и попробуй снова.")
        except Exception as e:
            return f"❌ OTA ошибка: {e}"

    # ── Flash других устройств через точку доступа ESP32 ───────────────
    def flash_via_ap(self, target_port: str, firmware_path: str,
                     chip: str = "esp32", baud: int = 460800) -> str:
        """
        Прошивает другой ESP32/ESP8266 через USB напрямую (esptool.py).
        Использует точку доступа ESP32 как управляющую консоль.
        """
        fw = Path(firmware_path)
        if not fw.exists():
            return f"❌ Прошивка не найдена: {firmware_path}"

        # Отправляем уведомление на дисплей ESP32
        self._send({"type": "reply", "text": f"Flash {chip} → {target_port}..."})

        cmd = [
            "esptool.py",
            "--chip", chip,
            "--port", target_port,
            "--baud", str(baud),
            "write_flash",
            "--flash_mode", "dio",
            "--flash_freq", "80m",
            "--flash_size", "detect",
            "0x0", str(fw)
        ]
        log.info("esptool: %s", " ".join(cmd))
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120
            )
            if result.returncode == 0:
                msg = f"✅ Flash OK: {fw.name} → {target_port}"
            else:
                msg = f"❌ esptool ошибка:\n{result.stderr[:300]}"
        except FileNotFoundError:
            msg = "❌ esptool.py не найден. Установи: pip install esptool --break-system-packages"
        except subprocess.TimeoutExpired:
            msg = "❌ Таймаут прошивки (>120 сек)"
        except Exception as e:
            msg = f"❌ Ошибка: {e}"

        self._send({"type": "reply", "text": msg[:200]})
        return msg

    # ── Список доступных портов ────────────────────────────────────────
    def list_ports(self) -> str:
        if not _SERIAL_OK:
            return "❌ pyserial не установлен"
        ports = list(serial.tools.list_ports.comports())
        if not ports:
            return "📭 USB-порты не найдены."
        lines = ["🔌 Доступные COM/USB порты:"]
        for p in ports:
            lines.append(f"  {p.device} — {p.description}")
        return "\n".join(lines)

    # ── Web UI ESP32 в браузере ARGOS ──────────────────────────────────
    def open_webui(self) -> str:
        url = f"http://{self._ap_ip}"
        try:
            import webbrowser
            webbrowser.open(url)
            return f"🌐 Открываю Web UI: {url}"
        except Exception:
            return f"🌐 Web UI доступен: {url}\nПодключись к Wi-Fi {self._ap_ssid}"


# ── Синглтон ────────────────────────────────────────────────────────────────
_bridge: ESP32UsbBridge | None = None


def handle(text: str, core=None) -> str:
    global _bridge
    t = text.lower().strip()

    # Инициализация
    if _bridge is None:
        _bridge = ESP32UsbBridge(core=core)

    # Команды управления мостом
    if any(k in t for k in ["подключи esp", "запусти мост", "esp32 мост", "esp32 старт", "esp bridge"]):
        # Извлекаем явный порт из текста: COM6, /dev/ttyUSB0, ttyACM0 и т.д.
        import re as _re_port
        _pm = _re_port.search(
            r'(/dev/tty\S+|COM\d+|ttyUSB\d+|ttyACM\d+)',
            text, _re_port.IGNORECASE
        )
        if _pm:
            _bridge.port = _pm.group(1)   # переопределяем порт перед стартом
        return _bridge.start()

    if any(k in t for k in ["отключи esp", "стоп мост", "esp32 стоп"]):
        return _bridge.stop()

    if any(k in t for k in ["статус esp", "esp32 статус", "мост статус"]):
        return _bridge.status()

    if any(k in t for k in ["порты usb", "список портов", "com порты"]):
        return _bridge.list_ports()

    if any(k in t for k in ["esp веб", "esp web", "открой esp"]):
        return _bridge.open_webui()

    # ── прошей esp <PORT> <file.ino|.bin> — реальная прошивка ────────────────
    if "прошей esp" in t:
        import re as _re, shutil as _sh, tempfile as _tmp, sys as _sys

        # Корень ARGOS и папка прошивок (абсолютный путь)
        _root   = Path(__file__).resolve().parent.parent.parent
        _fw_dir = _root / "assets" / "firmware"

        def _run(cmd, timeout=300):
            """subprocess.run — UTF-8 (arduino-cli/esptool оба Go/Python = UTF-8)."""
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=timeout,
                                   encoding="utf-8", errors="replace")
                return r.returncode, (r.stdout or "") + (r.stderr or "")
            except subprocess.TimeoutExpired:
                return -1, f"⏱ Тайм-аут {timeout}с"
            except FileNotFoundError:
                return -2, f"❌ Команда не найдена: {cmd[0]}"
            except Exception as ex:
                return -3, str(ex)

        # ── Извлекаем порт ───────────────────────────────────────────────────
        port_m = _re.search(r'(/dev/tty\S+|COM\d+|ttyUSB\d+|ttyACM\d+)', text, _re.IGNORECASE)
        port   = port_m.group(1) if port_m else None
        if not port:
            return ("❓ Укажи COM-порт.\n"
                    "  Пример: прошей esp COM6 argos_esp32_2432s024.bin\n"
                    "  Список портов: com порты")

        # ── Извлекаем имя/путь файла ─────────────────────────────────────────
        fw_m   = _re.search(r'([\w/\\:.\-]+\.(ino|bin|hex))', text, _re.IGNORECASE)
        fw_raw = fw_m.group(1) if fw_m else None
        if not fw_raw:
            available = "\n".join(f"  • {f.name}" for f in _fw_dir.glob("*.ino"))
            return (f"❓ Укажи файл прошивки.\n"
                    f"  Пример: прошей esp {port} argos_esp32_2432s024.ino\n"
                    f"  Доступные прошивки в assets/firmware:\n{available}")

        fw_path = Path(fw_raw)
        ext     = fw_path.suffix.lower()

        # ── Находим или создаём файл прошивки ────────────────────────────────
        if not fw_path.is_absolute() or not fw_path.exists():
            # Ищем по имени файла в assets/firmware (приоритет)
            candidate = _fw_dir / fw_path.name
            if candidate.exists():
                fw_path = candidate
            else:
                # Файла нет — генерируем через AirFlasher
                try:
                    from src.factory.flasher import AirFlasher as _AF, SMART_FIRMWARES as _SF
                    _fl = _AF()
                    sketch_name = fw_path.stem
                    # Ищем готовый шаблон по имени
                    _meta = next(
                        (v for v in _SF.values()
                         if v.get("kind") in ("ino", "auto") and
                            Path(v.get("path", "x")).stem == sketch_name),
                        None
                    )
                    if _meta:
                        p = Path(_meta["path"])
                        if p.exists():
                            fw_path = p
                        else:
                            fw_path = Path(_fl._build_firmware_stub("esp32", sketch_name))
                    else:
                        fw_path = Path(_fl._build_firmware_stub("esp32", sketch_name))
                except Exception as _ge:
                    return f"❌ Не удалось создать прошивку: {_ge}"

        if not fw_path.exists():
            return f"❌ Файл прошивки не найден и не создан: {fw_path}"

        # ── .bin / .hex — прямая прошивка ────────────────────────────────────
        if ext in (".bin", ".hex"):
            esptool = _sh.which("esptool.py") or _sh.which("esptool")
            if not esptool:
                return "❌ esptool не найден. Установи: pip install esptool"
            code, out = _run([esptool, "--port", port, "--baud", "921600",
                              "write_flash", "0x1000", str(fw_path)])
            if code == 0:
                return f"✅ ESP32 прошит: {fw_path.name} → {port}"
            return f"❌ esptool ошибка (код {code}):\n{out[:500]}"

        # ── .ino — компиляция + прошивка ─────────────────────────────────────
        if ext == ".ino":
            lines = []   # буфер строк ответа (был не инициализирован — NameError)
            arduino_cli = _sh.which("arduino-cli")

            # Если shutil.which не нашёл — ищем в типичных папках winget/scoop
            if not arduino_cli and _sys.platform == "win32":
                import glob as _glob
                _search_patterns = [
                    str(Path.home() / "AppData/Local/Microsoft/WinGet/Packages/ArduinoSA.CLI*/**/arduino-cli.exe"),
                    str(Path.home() / "scoop/apps/arduino-cli/current/arduino-cli.exe"),
                    "C:/Program Files/Arduino CLI/arduino-cli.exe",
                    "C:/Program Files (x86)/Arduino CLI/arduino-cli.exe",
                ]
                for _pat in _search_patterns:
                    _found = _glob.glob(_pat, recursive=True)
                    if _found:
                        arduino_cli = _found[0]
                        break

            if not arduino_cli:
                sn = fw_path.stem
                return (
                    f"⚠️ arduino-cli не найден в PATH и в стандартных папках.\n\n"
                    f"1. Найди где установлен:\n"
                    f"   Get-ChildItem $env:LOCALAPPDATA\\Microsoft\\WinGet\\Packages -Recurse -Filter arduino-cli.exe | Select FullName\n\n"
                    f"2. Добавь папку в PATH:\n"
                    f"   $env:PATH += ';C:\\путь\\к\\папке\\arduino-cli'\n\n"
                    f"3. Перезапусти ARGOS и повтори: прошей esp {port} {sn}.ino\n\n"
                    f"Или скомпилируй в Arduino IDE → Sketch → Export Compiled Binary\n"
                    f"  Потом: прошей esp {port} {sn}.ino.bin"
                )

            # arduino-cli требует папку с именем == имя скетча
            import shutil as _sh2
            sn       = fw_path.stem
            tmp_root = Path(_tmp.mkdtemp(prefix="argos_sketch_"))
            sdir     = tmp_root / sn
            sdir.mkdir()
            _sh2.copy2(str(fw_path), str(sdir / fw_path.name))
            bdir = Path(_tmp.mkdtemp(prefix="argos_build_"))

            # ── Проверяем ядро esp32:esp32, при необходимости ставим ────────────
            _cc, _cl = _run([arduino_cli, "core", "list"], timeout=30)
            _core_ok = _cc == 0 and "esp32:esp32" in _cl
            if not _core_ok:
                lines.append("⚙️ Ядро esp32:esp32 не найдено — устанавливаю "
                             "(скачивает ~200 МБ, займёт 5-15 мин)...")
                ic, io = _run([arduino_cli, "core", "install", "esp32:esp32"],
                              timeout=900)  # 15 мин
                if ic not in (0,):
                    return ("\n".join(lines) +
                            f"\n❌ Установка esp32:esp32 провалилась (код {ic}):\n{io[:400]}\n\n"
                            "Установи вручную в PowerShell, потом повтори:\n"
                            "  arduino-cli core install esp32:esp32")
                lines.append("✅ Ядро esp32:esp32 установлено")

            lines.append(f"🔨 Компилирую {fw_path.name}  (1-3 мин)...")
            code, out = _run(
                [arduino_cli, "compile", "--fqbn", "esp32:esp32:esp32",
                 "--output-dir", str(bdir), str(sdir)],
                timeout=300
            )
            if code != 0:
                return ("\n".join(lines) +
                        f"\n❌ Компиляция провалилась (код {code}):\n{out[:700]}")

            # Ищем .bin (не bootloader, не partitions)
            bins = [b for b in bdir.glob("*.bin")
                    if "bootloader" not in b.name and "partitions" not in b.name]
            if not bins:
                bins = list(bdir.glob("*.bin"))
            if not bins:
                return f"❌ Компиляция OK, но .bin не найден в {bdir}\n{out[:300]}"

            bin_path = bins[0]
            lines.append(f"✅ Скомпилировано: {bin_path.name}")
            lines.append(f"⚡ Прошиваю {port}...")

            esptool = _sh.which("esptool.py") or _sh.which("esptool")
            if not esptool:
                lines.append("❌ esptool не найден: pip install esptool")
                return "\n".join(lines)

            code2, out2 = _run(
                [esptool, "--port", port, "--baud", "921600",
                 "write_flash", "0x1000", str(bin_path)],
                timeout=120
            )
            if code2 == 0:
                lines.append(f"✅ ESP32 прошит успешно → {port}")
            else:
                lines.append(f"❌ esptool (код {code2}):\n{out2[:400]}")
            return "\n".join(lines)

        return f"⚠️ Неизвестный тип файла: {ext}. Укажи .bin или .ino"

    # OTA обновление
    if "ota" in t or "прошить esp" in t or "обнови esp" in t or "обнови esp32" in t:
        # Ищем путь к .bin файлу в тексте
        import re
        m = re.search(r'[\w/\\:.\-]+\.bin', text)
        if m:
            return _bridge.ota_update(m.group(0))
        # Ищем последний .bin в папке firmware
        fw_dir = Path(__file__).parent.parent.parent / "assets" / "firmware"
        bins = sorted(fw_dir.glob("*.bin"), key=lambda x: x.stat().st_mtime, reverse=True)
        if bins:
            return _bridge.ota_update(str(bins[0]))
        return "❓ Укажи путь к .bin файлу: 'прошить esp /path/to/fw.bin'"

    # Flash через esptool (flash esp / flash esp32 / flash COM3 fw.bin)
    if "flash esp" in t or "flash esp32" in t or "прошить порт" in t or \
       (("flash" in t or "прошить порт" in t) and any(k in t for k in ["com", "/dev/tty"])):
        import re
        port_m = re.search(r'(COM\d+|/dev/tty\S+)', text, re.IGNORECASE)
        bin_m  = re.search(r'[\w/\\:.\-]+\.bin', text)
        if port_m and bin_m:
            return _bridge.flash_via_ap(port_m.group(1), bin_m.group(0))
        return "❓ Формат: 'flash COM3 /path/to/fw.bin'"

    return None


def setup(core=None):
    pass
