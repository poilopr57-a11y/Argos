"""
smart_environments.py — Умные среды Аргоса
  Операторы: умный дом, теплица, гараж, погреб,
             инкубатор, аквариум, террариум.
  Каждый оператор: мониторинг + управление + алерты + автоматика.
"""

SKILL_DESCRIPTION = "Управление умным домом: сцены, окружения, IoT"

import time, os
from src.argos_logger import get_logger
from src.event_bus import get_bus, Events

log = get_logger("argos.smart")
bus = get_bus()


# ── БАЗОВЫЙ ОПЕРАТОР ──────────────────────────────────────
class SmartEnvironment:
    NAME = "environment"
    ICON = "🏠"
    # Пороги по умолчанию (переопределяются в подклассах)
    THRESHOLDS: dict = {}

    def __init__(self, iot_bridge=None):
        self.iot = iot_bridge
        self._alerts = {}  # metric → last_alert_ts
        self._rules = []  # автоматические правила

    def get_sensor(self, dev_id: str, key: str, default=None):
        if self.iot:
            return self.iot.get_value(dev_id, key)
        return default

    def send_cmd(self, dev_id: str, cmd: str, val=None) -> str:
        if self.iot:
            return self.iot.send_command(dev_id, cmd, val)
        return f"[SIM] {dev_id}.{cmd}={val}"

    def check_thresholds(self, readings: dict) -> list[str]:
        alerts = []
        now = time.time()
        for metric, value in readings.items():
            rule = self.THRESHOLDS.get(metric)
            if not rule or value is None:
                continue
            triggered = False
            if "max" in rule and value > rule["max"]:
                triggered = True
                msg = f"⚠️ {self.ICON} {self.NAME}: {metric}={value}{rule.get('unit','')} > макс {rule['max']}"
            elif "min" in rule and value < rule["min"]:
                triggered = True
                msg = f"⚠️ {self.ICON} {self.NAME}: {metric}={value}{rule.get('unit','')} < мин {rule['min']}"
            if triggered:
                last = self._alerts.get(metric, 0)
                if now - last > 300:  # кулдаун 5 мин
                    self._alerts[metric] = now
                    alerts.append(msg)
                    bus.emit(
                        Events.ENV_ALERT,
                        {"env": self.NAME, "metric": metric, "value": value},
                        "smart",
                    )
        return alerts

    def report(self) -> str:
        return f"{self.ICON} {self.NAME}: нет данных (IoT не подключён)"

    def status_line(self, dev_id: str, metrics: list, labels: dict) -> str:
        parts = []
        for m in metrics:
            v = self.get_sensor(dev_id, m)
            if v is not None:
                u = labels.get(m, "")
                parts.append(f"{m}={v}{u}")
        return ", ".join(parts) if parts else "нет данных"

    def add_rule(self, condition_fn, action_fn, name: str = "rule"):
        self._rules.append({"name": name, "cond": condition_fn, "action": action_fn})

    def run_rules(self) -> list[str]:
        results = []
        for rule in self._rules:
            try:
                if rule["cond"](self):
                    result = rule["action"](self)
                    if result:
                        results.append(f"⚙️ {rule['name']}: {result}")
            except Exception as e:
                log.error("Rule %s error: %s", rule["name"], e)
        return results


# ══════════════════════════════════════════════════════════
# 1. УМНЫЙ ДОМ
# ══════════════════════════════════════════════════════════
class SmartHome(SmartEnvironment):
    NAME = "Умный дом"
    ICON = "🏠"
    THRESHOLDS = {
        "temperature": {"min": 18, "max": 28, "unit": "°C"},
        "humidity": {"min": 30, "max": 70, "unit": "%"},
        "co2": {"max": 1000, "unit": "ppm"},
        "smoke": {"max": 0.1, "unit": ""},
        "motion": {},
    }

    def climate_report(self) -> str:
        lines = [f"{self.ICON} УМНЫЙ ДОМ — КЛИМАТ:"]
        rooms = ["living_room", "bedroom", "kitchen", "bathroom"]
        for room in rooms:
            t = self.get_sensor(f"th_{room}", "temperature")
            h = self.get_sensor(f"th_{room}", "humidity")
            if t is not None or h is not None:
                lines.append(f"  {room}: {t}°C, {h}% влажность")
        return "\n".join(lines) if len(lines) > 1 else f"{self.ICON} Датчики климата не подключены"

    def lights_on(self, room: str = "all") -> str:
        if room == "all":
            return "; ".join(
                [
                    self.send_cmd(f"light_{r}", "state", "ON")
                    for r in ["living", "bedroom", "kitchen"]
                ]
            )
        return self.send_cmd(f"light_{room}", "state", "ON")

    def lights_off(self, room: str = "all") -> str:
        if room == "all":
            return "; ".join(
                [
                    self.send_cmd(f"light_{r}", "state", "OFF")
                    for r in ["living", "bedroom", "kitchen"]
                ]
            )
        return self.send_cmd(f"light_{room}", "state", "OFF")

    def set_thermostat(self, temp: float) -> str:
        return self.send_cmd("thermostat", "setpoint", temp)

    def security_status(self) -> str:
        motion = self.get_sensor("motion_main", "motion")
        door_open = self.get_sensor("door_front", "contact")
        armed = self.get_sensor("alarm", "armed")
        return (
            f"{self.ICON} БЕЗОПАСНОСТЬ:\n"
            f"  Движение:  {'🔴 Обнаружено' if motion else '🟢 Нет'}\n"
            f"  Дверь:     {'🔓 Открыта' if door_open else '🔒 Закрыта'}\n"
            f"  Сигнализация: {'🔔 ВКЛ' if armed else '🔕 ВЫКЛ'}"
        )

    def report(self) -> str:
        return f"{self.climate_report()}\n{self.security_status()}"


# ══════════════════════════════════════════════════════════
# 2. УМНАЯ ТЕПЛИЦА
# ══════════════════════════════════════════════════════════
class SmartGreenhouse(SmartEnvironment):
    NAME = "Теплица"
    ICON = "🌿"
    THRESHOLDS = {
        "temperature": {"min": 15, "max": 35, "unit": "°C"},
        "humidity": {"min": 60, "max": 95, "unit": "%"},
        "soil_moisture": {"min": 30, "max": 80, "unit": "%"},
        "co2": {"min": 400, "max": 1500, "unit": "ppm"},
        "light": {"min": 2000, "max": 80000, "unit": " lux"},
        "ph": {"min": 5.5, "max": 7.0, "unit": "pH"},
    }

    def irrigation(self, zone: int = 1, duration_sec: int = 30) -> str:
        result = self.send_cmd(f"valve_zone{zone}", "open", duration_sec)
        log.info("Теплица: полив зона %d, %dс", zone, duration_sec)
        bus.emit("env.irrigation", {"zone": zone, "duration": duration_sec}, "greenhouse")
        return f"💧 Полив зоны {zone}: {duration_sec}с — {result}"

    def ventilation(self, mode: str = "auto") -> str:
        return self.send_cmd("ventilation", "mode", mode)

    def heating(self, setpoint: float = 22.0) -> str:
        return self.send_cmd("heater", "setpoint", setpoint)

    def lighting(self, on: bool = True, brightness: int = 100) -> str:
        return self.send_cmd(
            "grow_lights", "state", {"power": "ON" if on else "OFF", "brightness": brightness}
        )

    def auto_cycle(self) -> str:
        """Автоматический цикл: проверяет условия и управляет системами."""
        actions = []
        temp = self.get_sensor("env_sensor", "temperature")
        soil = self.get_sensor("soil_sensor", "soil_moisture")
        light = self.get_sensor("light_sensor", "light")
        if temp and temp > 30:
            actions.append(self.ventilation("max"))
        if soil and soil < 35:
            actions.append(self.irrigation(1, 60))
        if light and light < 3000:
            actions.append(self.lighting(True, 80))
        return "\n".join(actions) if actions else "✅ Теплица в норме, автоматика не требуется."

    def report(self) -> str:
        temp = self.get_sensor("env_sensor", "temperature") or "—"
        hum = self.get_sensor("env_sensor", "humidity") or "—"
        soil = self.get_sensor("soil_sensor", "soil_moisture") or "—"
        co2 = self.get_sensor("env_sensor", "co2") or "—"
        lux = self.get_sensor("light_sensor", "light") or "—"
        return (
            f"{self.ICON} ТЕПЛИЦА:\n"
            f"  Температура: {temp}°C  Влажность: {hum}%\n"
            f"  Почва:       {soil}%   CO₂: {co2} ppm\n"
            f"  Освещение:   {lux} lux"
        )


# ══════════════════════════════════════════════════════════
# 3. УМНЫЙ ГАРАЖ
# ══════════════════════════════════════════════════════════
class SmartGarage(SmartEnvironment):
    NAME = "Гараж"
    ICON = "🚗"
    THRESHOLDS = {
        "temperature": {"min": -5, "max": 40, "unit": "°C"},
        "co": {"max": 50, "unit": " ppm"},  # угарный газ
        "motion": {},
        "flood": {"max": 0, "unit": ""},
    }

    def door_open(self) -> str:
        return self.send_cmd("garage_door", "state", "OPEN")

    def door_close(self) -> str:
        return self.send_cmd("garage_door", "state", "CLOSE")

    def door_status(self) -> str:
        state = self.get_sensor("garage_door", "state") or "неизвестно"
        return f"🚗 Ворота гаража: {state}"

    def ventilation(self, on: bool = True) -> str:
        return self.send_cmd("garage_fan", "state", "ON" if on else "OFF")

    def heating(self, setpoint: float = 5.0) -> str:
        return self.send_cmd("garage_heater", "setpoint", setpoint)

    def report(self) -> str:
        door = self.get_sensor("garage_door", "state") or "—"
        temp = self.get_sensor("garage_climate", "temperature") or "—"
        co = self.get_sensor("garage_co", "co") or "—"
        motion = self.get_sensor("garage_motion", "motion")
        return (
            f"{self.ICON} ГАРАЖ:\n"
            f"  Ворота:     {door}\n"
            f"  Температура:{temp}°C  CO: {co} ppm\n"
            f"  Движение:   {'Да 🔴' if motion else 'Нет 🟢'}"
        )


# ══════════════════════════════════════════════════════════
# 4. УМНЫЙ ПОГРЕБ
# ══════════════════════════════════════════════════════════
class SmartCellar(SmartEnvironment):
    NAME = "Погреб"
    ICON = "🍾"
    THRESHOLDS = {
        "temperature": {"min": 2, "max": 12, "unit": "°C"},
        "humidity": {"min": 70, "max": 95, "unit": "%"},
        "flood": {"max": 0, "unit": ""},
        "methane": {"max": 10, "unit": " ppm"},
    }

    def ventilation(self, on: bool = True) -> str:
        return self.send_cmd("cellar_fan", "state", "ON" if on else "OFF")

    def lighting(self, on: bool = True) -> str:
        return self.send_cmd("cellar_light", "state", "ON" if on else "OFF")

    def report(self) -> str:
        temp = self.get_sensor("cellar_climate", "temperature") or "—"
        hum = self.get_sensor("cellar_climate", "humidity") or "—"
        flood = self.get_sensor("cellar_flood", "flood")
        return (
            f"{self.ICON} ПОГРЕБ:\n"
            f"  Температура: {temp}°C  Влажность: {hum}%\n"
            f"  Затопление:  {'🔴 ТРЕВОГА!' if flood else '🟢 Нет'}"
        )


# ══════════════════════════════════════════════════════════
# 5. ИНКУБАТОР
# ══════════════════════════════════════════════════════════
class SmartIncubator(SmartEnvironment):
    NAME = "Инкубатор"
    ICON = "🥚"
    THRESHOLDS = {
        "temperature": {"min": 37.2, "max": 37.8, "unit": "°C"},  # ±0.3°C
        "humidity": {"min": 55, "max": 65, "unit": "%"},
        "co2": {"max": 2000, "unit": " ppm"},
    }
    # Расписание переворота яиц
    TURN_INTERVAL_H = 3

    def __init__(self, iot_bridge=None):
        super().__init__(iot_bridge)
        self._incubation_start = None
        self._last_turn = 0

    def start_incubation(self, species: str = "chicken") -> str:
        PROFILES = {
            "chicken": {"temp": 37.5, "hum_1_18": 60, "hum_19_21": 75, "days": 21},
            "duck": {"temp": 37.8, "hum_1_25": 55, "hum_26_28": 75, "days": 28},
            "quail": {"temp": 37.5, "hum_1_15": 60, "hum_16_17": 75, "days": 17},
            "goose": {"temp": 37.5, "hum_1_28": 55, "hum_29_31": 75, "days": 31},
        }
        profile = PROFILES.get(species, PROFILES["chicken"])
        self._incubation_start = time.time()
        self.send_cmd("incubator_heater", "setpoint", profile["temp"])
        self.send_cmd(
            "incubator_humidifier", "target", profile.get("hum_1_18", profile.get("hum_1_25", 60))
        )
        bus.emit("env.incubation_start", {"species": species, "days": profile["days"]}, "incubator")
        return (
            f"{self.ICON} Инкубация запущена!\n"
            f"  Вид: {species}  Дней: {profile['days']}\n"
            f"  Температура: {profile['temp']}°C\n"
            f"  Влажность: {profile.get('hum_1_18', 60)}% → {profile.get('hum_19_21', 75)}%"
        )

    def turn_eggs(self) -> str:
        now = time.time()
        if now - self._last_turn < self.TURN_INTERVAL_H * 3600:
            h = int((self.TURN_INTERVAL_H * 3600 - (now - self._last_turn)) / 60)
            return f"⏰ Следующий переворот через {h} мин"
        self.send_cmd("egg_turner", "turn", "1")
        self._last_turn = now
        log.info("Инкубатор: яйца перевёрнуты")
        return f"{self.ICON} Яйца перевёрнуты ✅"

    def day_number(self) -> int:
        if not self._incubation_start:
            return 0
        return int((time.time() - self._incubation_start) / 86400) + 1

    def report(self) -> str:
        temp = self.get_sensor("incubator_sensor", "temperature") or "—"
        hum = self.get_sensor("incubator_sensor", "humidity") or "—"
        day = self.day_number()
        turn = int((time.time() - self._last_turn) / 60) if self._last_turn else "—"
        return (
            f"{self.ICON} ИНКУБАТОР:\n"
            f"  Температура: {temp}°C  Влажность: {hum}%\n"
            f"  День инкубации: {day}\n"
            f"  Последний переворот: {turn} мин назад"
        )


# ══════════════════════════════════════════════════════════
# 6. АКВАРИУМ
# ══════════════════════════════════════════════════════════
class SmartAquarium(SmartEnvironment):
    NAME = "Аквариум"
    ICON = "🐠"
    THRESHOLDS = {
        "temperature": {"min": 24, "max": 28, "unit": "°C"},
        "ph": {"min": 6.5, "max": 7.5, "unit": " pH"},
        "tds": {"min": 100, "max": 400, "unit": " ppm"},  # мутность
        "ammonia": {"max": 0.25, "unit": " ppm"},
        "nitrate": {"max": 20, "unit": " ppm"},
        "oxygen": {"min": 6.0, "unit": " mg/L"},
    }

    def feeding(self, portions: int = 1) -> str:
        result = self.send_cmd("aqua_feeder", "dispense", portions)
        bus.emit("env.aquarium_feed", {"portions": portions}, "aquarium")
        return f"{self.ICON} Кормление: {portions} порций — {result}"

    def water_change(self, percent: int = 20) -> str:
        # Открываем слив, потом заливаем
        self.send_cmd("aqua_drain", "open", "30s")
        self.send_cmd("aqua_fill", "open", "30s")
        return f"{self.ICON} Подмена воды: {percent}% инициирована"

    def lights_schedule(self, on_h: int = 8, off_h: int = 20) -> str:
        return self.send_cmd(
            "aqua_lights", "schedule", {"on": f"{on_h:02d}:00", "off": f"{off_h:02d}:00"}
        )

    def co2_inject(self, on: bool = True) -> str:
        return self.send_cmd("aqua_co2", "state", "ON" if on else "OFF")

    def report(self) -> str:
        temp = self.get_sensor("aqua_temp", "temperature") or "—"
        ph = self.get_sensor("aqua_ph", "ph") or "—"
        tds = self.get_sensor("aqua_tds", "tds") or "—"
        nh3 = self.get_sensor("aqua_chem", "ammonia") or "—"
        o2 = self.get_sensor("aqua_o2", "oxygen") or "—"
        return (
            f"{self.ICON} АКВАРИУМ:\n"
            f"  Температура: {temp}°C  pH: {ph}\n"
            f"  TDS: {tds} ppm  O₂: {o2} mg/L\n"
            f"  Аммиак: {nh3} ppm"
        )


# ══════════════════════════════════════════════════════════
# 7. ТЕРРАРИУМ
# ══════════════════════════════════════════════════════════
class SmartTerrarium(SmartEnvironment):
    NAME = "Террариум"
    ICON = "🦎"
    THRESHOLDS = {
        "temperature_warm": {"min": 28, "max": 38, "unit": "°C"},  # тёплый угол
        "temperature_cool": {"min": 22, "max": 28, "unit": "°C"},  # холодный угол
        "humidity": {"min": 60, "max": 90, "unit": "%"},
        "uvi": {"min": 2, "max": 8, "unit": " UVI"},
    }

    def __init__(self, iot_bridge=None, species: str = "bearded_dragon"):
        super().__init__(iot_bridge)
        self.species = species
        PROFILES = {
            "bearded_dragon": {"basking": 38, "cool": 26, "night": 22, "uvi": 6},
            "leopard_gecko": {"basking": 32, "cool": 24, "night": 20, "uvi": 2},
            "chameleon": {"basking": 30, "cool": 22, "night": 18, "uvi": 7},
            "ball_python": {"basking": 35, "cool": 27, "night": 25, "uvi": 0},
        }
        self.profile = PROFILES.get(species, PROFILES["bearded_dragon"])

    def basking_on(self) -> str:
        return self.send_cmd("terr_basking", "state", "ON")

    def uv_on(self) -> str:
        return self.send_cmd("terr_uv", "state", "ON")

    def misting(self, duration_sec: int = 10) -> str:
        return self.send_cmd("terr_mister", "spray", duration_sec)

    def night_mode(self) -> str:
        self.send_cmd("terr_basking", "state", "OFF")
        self.send_cmd("terr_uv", "state", "OFF")
        self.send_cmd("terr_red_lamp", "state", "ON")  # ночной подогрев
        return f"{self.ICON} Ночной режим активирован ({self.species})"

    def day_mode(self) -> str:
        self.send_cmd("terr_basking", "state", "ON")
        self.send_cmd("terr_uv", "state", "ON")
        self.send_cmd("terr_red_lamp", "state", "OFF")
        return f"{self.ICON} Дневной режим активирован. Basking={self.profile['basking']}°C"

    def report(self) -> str:
        tw = self.get_sensor("terr_warm", "temperature") or "—"
        tc = self.get_sensor("terr_cool", "temperature") or "—"
        hum = self.get_sensor("terr_hum", "humidity") or "—"
        uvi = self.get_sensor("terr_uv", "uvi") or "—"
        return (
            f"{self.ICON} ТЕРРАРИУМ ({self.species}):\n"
            f"  Тёплый угол: {tw}°C  Холодный: {tc}°C\n"
            f"  Влажность:   {hum}%  UVI: {uvi}"
        )


# ══════════════════════════════════════════════════════════
# МЕНЕДЖЕР УМНЫХ СРЕД
# ══════════════════════════════════════════════════════════
class SmartEnvironmentManager:
    def __init__(self, iot_bridge=None):
        self.iot = iot_bridge
        self.home = SmartHome(iot_bridge)
        self.greenhouse = SmartGreenhouse(iot_bridge)
        self.garage = SmartGarage(iot_bridge)
        self.cellar = SmartCellar(iot_bridge)
        self.incubator = SmartIncubator(iot_bridge)
        self.aquarium = SmartAquarium(iot_bridge)
        self.terrarium = SmartTerrarium(iot_bridge)

        self._envs = {
            "дом": self.home,
            "теплица": self.greenhouse,
            "гараж": self.garage,
            "погреб": self.cellar,
            "инкубатор": self.incubator,
            "аквариум": self.aquarium,
            "террариум": self.terrarium,
        }
        log.info("SmartEnvironmentManager: %d сред", len(self._envs))

    def full_report(self) -> str:
        lines = ["🏘️ УМНЫЕ СРЕДЫ АРГОСА:\n"]
        for name, env in self._envs.items():
            lines.append(env.report())
            lines.append("")
        return "\n".join(lines)

    def get_env(self, name: str) -> SmartEnvironment | None:
        for k, v in self._envs.items():
            if k in name.lower() or name.lower() in k:
                return v
        return None

    def _ha_climate_temp(self, entity_id: str, temp: int) -> str:
        import os, urllib.request, json
        HA = os.getenv("HA_URL", "http://localhost:8123")
        TOKEN = os.getenv("HA_TOKEN", "")
        if not TOKEN: return f"[SIM] setpoint={temp}"
        payload = json.dumps({"entity_id": entity_id, "temperature": temp}).encode()
        try:
            req = urllib.request.Request(
                f"{HA}/api/services/climate/set_temperature",
                data=payload,
                headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
                method="POST"
            )
            urllib.request.urlopen(req, timeout=5)
            return f"✅ Пол: температура {temp}°C"
        except Exception as e:
            return f"❌ {e}"

    def _ha_status(self) -> str:
        import os, urllib.request, json
        HA = os.getenv("HA_URL", "http://localhost:8123")
        TOKEN = os.getenv("HA_TOKEN", "")
        if not TOKEN: return "HA_TOKEN не задан"
        try:
            req = urllib.request.Request(f"{HA}/api/states",
                headers={"Authorization": f"Bearer {TOKEN}"})
            with urllib.request.urlopen(req, timeout=8) as r:
                states = json.loads(r.read())
            lights = {x['entity_id']: x['state'] for x in states if x['entity_id'].startswith('light.')}
            on_lights = [x['attributes'].get('friendly_name', eid) for eid, st in lights.items() for x in states if x['entity_id'] == eid and st == 'on']
            temp = next((x['state'] for x in states if 'temperature_humidity_sensor_temperature' in x['entity_id']), '?')
            hum = next((x['state'] for x in states if 'humidity' in x['entity_id'] and x['state'] not in ('unavailable','unknown')), '?')
            door = next((x['state'] for x in states if 'dver_door' in x['entity_id']), '?')
            pol = next((x['state'] for x in states if x['entity_id'] == 'climate.pol'), '?')
            return (f"🏠 Дом:\n"
                    f"💡 Свет: {', '.join(on_lights) or 'всё выключено'}\n"
                    f"🌡 Температура: {temp}°C, влажность: {hum}%\n"
                    f"🚪 Дверь: {'открыта' if door == 'on' else 'закрыта'}\n"
                    f"🔥 Тёплый пол: {pol}")
        except Exception as e:
            return f"❌ {e}"

    def _ha_call(self, service: str, entity_id: str) -> str:
        """Реальный вызов Home Assistant API."""
        import os, urllib.request, json
        HA = os.getenv("HA_URL", "http://localhost:8123")
        TOKEN = os.getenv("HA_TOKEN", "")
        if not TOKEN:
            return "[SIM] HA_TOKEN не задан"
        domain, svc = service.split(".")
        payload = json.dumps({"entity_id": entity_id}).encode()
        try:
            req = urllib.request.Request(
                f"{HA}/api/services/{domain}/{svc}",
                data=payload,
                headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
                method="POST"
            )
            urllib.request.urlopen(req, timeout=5)
            return f"✅ {service} → {entity_id}"
        except Exception as e:
            return f"❌ HA: {e}"

    def _ha_light(self, action: str, room: str = "") -> str:
        """Управление светом через HA API."""
        import os, urllib.request, json
        HA = os.getenv("HA_URL", "http://localhost:8123")
        TOKEN = os.getenv("HA_TOKEN", "")
        if not TOKEN:
            return self.home.lights_on(room) if action == "on" else self.home.lights_off(room)
        # Маппинг комнат → entity_id HA
        _ROOM_MAP = {
            "коридор": "light.dom_switch_1",
            "коридоре": "light.dom_switch_1",
            "туалет": "light.dom_switch_2",
            "туалете": "light.dom_switch_2",
            "кухня": "light.dom_switch_3",
            "кухне": "light.dom_switch_3",
            "ванная": "light.dom_switch_4",
            "ванной": "light.dom_switch_4",
            "подсветка": "light.dom_backlight",
            "подсветку": "light.dom_backlight",
        }
        if room and room in _ROOM_MAP:
            eid = _ROOM_MAP[room]
            return self._ha_call(f"light.turn_{action}", eid)
        elif not room or room == "all":
            # Все световые группы
            results = []
            for nm, eid in _ROOM_MAP.items():
                r = self._ha_call(f"light.turn_{action}", eid)
                if "✅" in r:
                    results.append(nm)
            return f"✅ Свет {'включён' if action=='on' else 'выключен'}: {', '.join(results) or 'везде'}"
        else:
            return f"❓ Комната '{room}' не найдена. Доступно: {', '.join(_ROOM_MAP.keys())}"

    def process_command(self, text: str) -> str | None:
        t = text.lower()

        # ── Управление светом (реальное HA API) ────────────────────
        _is_on  = any(w in t for w in ("включи", "включить", "вкл", "зажги"))
        _is_off = any(w in t for w in ("выключи", "выключить", "выкл", "погаси", "потуши"))
        if _is_on or _is_off:
            if any(w in t for w in ("свет", "лампу", "освещение", "весь", "все")):
                room = self._extract_room(t)
                return self._ha_light("on" if _is_on else "off", room)

        # ── Розетка ПК ──────────────────────────────────────────────
        if any(w in t for w in ("розетку", "розетка", "комп", "пк", "компьютер")) and not "разетку" in t:
            action = "on" if _is_on else ("off" if _is_off else None)
            if action:
                return self._ha_call(f"switch.turn_{action}", "switch.dian_nao_socket_1")

        # ── Тёплый пол ──────────────────────────────────────────────
        if any(w in t for w in ("пол", "тёплый", "теплый", "обогрев")):
            if _is_on:
                return self._ha_call("climate.turn_on", "climate.pol")
            elif _is_off:
                return self._ha_call("climate.turn_off", "climate.pol")
            elif "температур" in t:
                import re
                nums = re.findall(r'\d+', t)
                if nums:
                    temp = int(nums[0])
                    return self._ha_climate_temp("climate.pol", temp)

        # ── Сцены ────────────────────────────────────────────────────
        if "вечерний" in t or "вечер" in t:
            r = self._ha_call("scene.turn_on", "scene.вечерний_режим")
            if "❌" in r:  # Fallback
                self._ha_light("off", "коридор"); self._ha_light("off", "туалет")
                self._ha_light("on", "кухня"); self._ha_light("on", "ванная")
                return "✅ Вечерний режим: кухня + ванная"
            return r
        if "утренний" in t or "утро" in t or "рабочий" in t:
            self._ha_light("on", ""); # всё
            return "✅ Утренний режим: весь свет включён"

        # ── Статус дома ──────────────────────────────────────────────
        if any(w in t for w in ("статус", "состояние", "дом", "что включено")):
            return self._ha_status()

        # Старые паттерны
        if "свет включи" in t or "свет вкл" in t:
            room = self._extract_room(t)
            return self._ha_light("on", room)
        if "свет выключи" in t or "свет выкл" in t:
            room = self._extract_room(t)
            return self._ha_light("off", room)
        if "температура дома" in t or "климат дома" in t:
            return self.home.climate_report()
        if "безопасность" in t or "охрана" in t:
            return self.home.security_status()
        # Теплица
        if "полив" in t:
            zone = 1
            for w in t.split():
                if w.isdigit():
                    zone = int(w)
                    break
            return self.greenhouse.irrigation(zone, 60)
        if "теплица авто" in t or "автополив" in t:
            return self.greenhouse.auto_cycle()
        if "отчёт теплица" in t or "теплица статус" in t:
            return self.greenhouse.report()
        # Гараж
        if "ворота открой" in t or "открой гараж" in t:
            return self.garage.door_open()
        if "ворота закрой" in t or "закрой гараж" in t:
            return self.garage.door_close()
        if "гараж статус" in t:
            return self.garage.report()
        # Инкубатор
        if "инкубатор старт" in t or "запусти инкубатор" in t:
            species = "chicken"
            for s in ["chicken", "duck", "quail", "goose", "курица", "утка", "перепел", "гусь"]:
                if s in t:
                    species = {
                        "курица": "chicken",
                        "утка": "duck",
                        "перепел": "quail",
                        "гусь": "goose",
                    }.get(s, s)
            return self.incubator.start_incubation(species)
        if "переверни яйца" in t or "перевернуть яйца" in t:
            return self.incubator.turn_eggs()
        if "инкубатор статус" in t:
            return self.incubator.report()
        # Аквариум
        if "покорми рыб" in t or "кормление аквариум" in t:
            return self.aquarium.feeding()
        if "подмена воды" in t:
            return self.aquarium.water_change()
        if "аквариум статус" in t:
            return self.aquarium.report()
        # Террариум
        if "ночной режим" in t or "ночь террариум" in t:
            return self.terrarium.night_mode()
        if "дневной режим" in t or "день террариум" in t:
            return self.terrarium.day_mode()
        if "опрыскивание" in t or "опрыскай" in t:
            return self.terrarium.misting(15)
        if "террариум статус" in t:
            return self.terrarium.report()
        # Полный отчёт
        if any(k in t for k in ["умные среды", "все системы", "полный отчёт"]):
            return self.full_report()
        # Статус отдельных сред
        if any(k in t for k in ["умный дом статус", "дом статус", "home status"]):
            return self.home.report()
        if any(k in t for k in ["погреб статус", "cellar status"]):
            return self.cellar.report()
        if any(k in t for k in ["аквариум статус", "aquarium status"]):
            return self.aquarium.report()
        return None

    def _extract_room(self, text: str) -> str:
        _ROOMS = {
            "кухня": "кухня", "кухне": "кухня", "кухню": "кухня",
            "коридор": "коридор", "коридоре": "коридор", "коридору": "коридор", "колидор": "коридор",
            "туалет": "туалет", "туалете": "туалет",
            "ванная": "ванная", "ванной": "ванная", "ванну": "ванная",
            "подсветка": "подсветка", "подсветку": "подсветка",
        }
        for word, room in _ROOMS.items():
            if word in text:
                return room
        return ""  # пустая строка = вся квартира


# Alias для совместимости с skill_loader и core.py
class SmartEnvironmentsSkill:
    """Фасад — обёртка над SmartEnvironmentManager для интеграции в core."""

    def __init__(self, mgr: "SmartEnvironmentManager" = None):
        self.mgr = mgr or SmartEnvironmentManager()

    def list_systems(self) -> str:
        return self.mgr.full_report()

    def handle(self, text: str) -> str:
        return self.mgr.process_command(text) or "❓ Команда не распознана."

    def full_report(self) -> str:
        return self.mgr.full_report()

    def execute(self, text: str = "") -> str:
        """Точка входа SkillLoader."""
        t = (text or "").strip()
        if not t or t.lower() in ("статус", "status", "отчёт", "отчет"):
            return self.full_report()
        return self.handle(t)


TRIGGERS = [
    "умный дом", "умные среды",
    "свет включи", "свет выключи", "включи свет", "выключи свет",
    "включить свет", "выключить свет",
    "климат дома", "температура дома", "полив", "теплица",
    "гараж", "погреб", "инкубатор", "аквариум", "безопасность дома",
    "ворота", "автополив", "полный отчёт",
    "кухне", "зале", "спальне", "туалете",  # комнаты
]

_skill_instance = None


def handle(text: str, core=None) -> str | None:
    global _skill_instance
    t = text.lower()
    if not any(tr in t for tr in TRIGGERS):
        return None
    if _skill_instance is None:
        _skill_instance = SmartEnvironmentsSkill()
    result = _skill_instance.mgr.process_command(text)
    # Если команда не распознана — возвращаем None чтобы ИИ мог ответить
    return result if result is not None else None


def setup(core=None):
    pass
