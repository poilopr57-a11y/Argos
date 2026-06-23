# VIBE — Расширенный контекст ARGOS (полная шпаргалка)

## Как работать с ARGOS из бук-клиента

### SSH на ПК
```bash
# Динамический IP — сканировать если нет связи
nmap -p 5001,8082 192.168.1.0/24
ssh -i /home/ava/.ssh/id_ed25519 AvA@192.168.1.72
```

### Проверка здоровья ARGOS
```bash
curl -s http://192.168.1.72:5001/brain/nodes | python3 -c "import sys,json; [print(n['node_id'],n['status']) for n in json.load(sys.stdin)['nodes']]"
```

### Проверка xiaozhi-server
```bash
curl -s http://192.168.1.72:8006/health
```

### Проверка llama-server
```bash
curl -s http://192.168.1.72:8082/health
curl -s http://192.168.1.72:8085/health
```

## Аварийные действия

### Перезапуск xiaozhi-server
```powershell
# PowerShell на ПК
Stop-Process -Name python -Force -ErrorAction SilentlyContinue
Set-Location F:\debug\argoss\xiaozhi-server
Start-Process -FilePath F:\debug\argoss\.venv\Scripts\python.exe -ArgumentList server.py -WorkingDirectory F:\debug\argoss\xiaozhi-server -WindowStyle Hidden
```

### main.py упал / завис на Ollama
- Ollama отключена на ПК (`ollama.exe.disabled`).
- main.py ждёт `localhost:11434` — этот таймаут длинный.
- Если Ollama не нужна, отключить в `.env` или запускать main.py в режиме без Ollama.

### OTA-петля ESP32-S3
- Причина: `XIAOZHI_FIRMWARE_FORCE=1` в `.env`.
- Фикс: после успешной прошивки изменить на `XIAOZHI_FIRMWARE_FORCE=0`.

### Дубли процессов
- Симптом: 2 `server.py`, 2 `entity_council.py`.
- Причина: Startup folder + Scheduled Task оба запускают один и тот же сервис.
- Фикс: убить дубли, оставить один канал запуска.

## Последние факты (2026-06-23)

1. `main.py` с ПК скопирован на бук и защищён от перезаписи.
2. `start_local_gpu.ps1` исправлен: правильный путь к `.venv`.
3. `.env` ПК забэкаплен на бук: `/home/ava/Projects/argoss/.env.pc.20260623_141901.bak`.
4. xiaozhi-server работает на `localhost:8006` ПК.
5. main.py ПК отвечает на `localhost:8010`.
6. Telegram-боты ARGOS: `BOT_IDS=8753655441,8685383341,8110060850,8997664457,8762695804,8827177286,8457185900`.
7. Entity Council group: `-1003844162784`.
8. llama-server: 8082 (RX580 Vulkan), 8085 (V100 CUDA mistral-nemo 12B).

## Рабочие директории

| Система | Путь |
|---|---|
| ПК ARGOS | `F:\debug\argoss` |
| Vault ПК | `F:\debug\аргос` |
| Бук ARGOS | `/home/ava/Projects/argoss` |
| Vault бук | `/home/ava/Documents/MyObsidianVault/SharedMemory/shared/` |
| Бэкап .env | `/home/ava/Projects/argoss/.env.pc.20260623_141901.bak` |
| Бэкап прошивки | `/home/ava/Projects/argoss/xiaozhi_argos_gui.bin` |

## Контакты и навыки Hermes

- `argos-vibe-topology` — правило разделения ролей.
- `argos-real-world-devops` — live-system state, GPU fleet.
- `agent-harness-generation` — кастомный harness.
- `hermes-agent` — настройка Hermes.
- `xiaozhi-voice-ops` — управление ESP32-S3 сервером.

## Ava Protocol

AI = observation, human = meaning, protocol = boundary.
