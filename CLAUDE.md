# Memory

## Me
Всеволод (Seva / АvA / SiG) — разработчик, автор проекта ARGOS.

## Projects
| Name | What |
|------|------|
| **ARGOS** | Argos Universal OS v2.1.3 — самовоспроизводящаяся кроссплатформенная AI-экосистема (Desktop / Android / Docker / Telegram) |

## Teams / Owners
Всё один человек (Сева). Роли — контекст задачи, не реальные люди.
| Tag | Контекст |
|-----|---------|
| `infra` | Инфраструктура (Llama.cpp, Ollama, RAM-диск) |
| `platform` | Платформа (Redis, aiohttp, PostgreSQL, Cloudflare) |
| `sre` | Надёжность (circuit breaker, watchtower, ArgoCD) |
| `app` | Приложение (Docker-изоляция, логирование) |

## Key Terms
| Term | Meaning |
|------|---------|
| `web_learn` | Модуль поиска через DuckDuckGo |
| `Llama.cpp` | Локальный оффлайн LLM-движок |
| `Ollama` | Запускатель LLM-моделей |
| `ArgoCD` | GitOps-инструмент синхронизации конфигураций |
| `watchtower` | Авто-обновлятор Docker-контейнеров |
| `circuit breaker` | Паттерн отказоустойчивости (3 неудачи → fallback) |
| `AWA-Core` | Центральный координатор модулей ARGOS |
| `ColibriAsmEngine` | Ассемблер/дизассемблер микрокода в реальном времени |

## Preferences
- Язык задач: русский
- Приоритеты: P1 (критичные) → P2 (важные) → P3 (низкий приоритет)


---

## SharedMemory (РћР±С‰Р°СЏ РїР°РјСЏС‚СЊ)

- **Р§РёС‚Р°С‚СЊ РїСЂРё РєР°Р¶РґРѕРј СЃС‚Р°СЂС‚Рµ:** `C:\Users\AvA\OneDrive\ObsidianShared\SharedMemory\shared\SHARED.md`
- **РЎРІРѕСЏ РїР°РїРєР° РїР°РјСЏС‚Рё:** `C:\Users\AvA\OneDrive\ObsidianShared\SharedMemory\claude\`
- **РЎРёРЅС…СЂРѕРЅРёР·Р°С†РёСЏ:** Р°РІС‚РѕРјР°С‚РёС‡РµСЃРєРё РєР°Р¶РґС‹Рµ 2 РјРёРЅ СЃ РЅРѕСѓС‚Р±СѓРєРѕРј (Arch Linux)
- **Р’СЃРµ Р°РіРµРЅС‚С‹:** claude/, argos/, opencode/, ollama/ вЂ” РєР°Р¶РґС‹Р№ РїРёС€РµС‚ РІ СЃРІРѕСЋ РїР°РїРєСѓ

## ⚠️ Урок 2026-05-31: onnxruntime segfault
`onnxruntime 1.24.4` (тянется с faster-whisper) вызывает **segmentation fault** при
многопоточной загрузке skills в main.py. Симптом: main молча падает (segfault) на
случайном skill, в логе только "[MemPalace] NDArray circular import".
ФИКС: `pip uninstall onnxruntime` + прогрев нативных либ (numpy/torch/ctranslate2/cv2)
в ГЛАВНОМ потоке main.py ДО загрузки skills (см. начало main.py).
НЕ ставить onnxruntime обратно без проверки старта ARGOS.

## ⚠️ Урок 2026-05-31: MemPalace = sqlite+numpy (НЕ chromadb)
chromadb 0.6.3 на этой машине нестабилен:
- при `import chromadb` инстанцирует DefaultEmbeddingFunction (ONNXMiniLM) → требует
  onnxruntime (которого нет) → ValueError на импорте;
- его нативный producer-поток даёт `resource deadlock would occur` и **SEGFAULT (139)**
  на большой базе (453 МБ, 31k эмбеддингов).
ФИКС:
1. Стаб `.venv/Lib/site-packages/onnxruntime/__init__.py` (pure-python, без нативной
   либы) — чтобы `import chromadb`/прочее не падало. НЕ удалять.
2. MemPalace переведён на СВОЙ backend `_VecStore` (sqlite3+numpy, детерминированные
   эмбеддинги) в `src/mempalace_bridge.py`. Хранилище: `data/mempalace/mempalace_vec.sqlite3`.
   Авто-миграция из старого chroma.sqlite3 (30983 записи). store+search+dedup, без segfault.
3. `ARGOS_VECTOR_FORCE_FALLBACK=1` в .env — vector_store НЕ создаёт chromadb-клиент.
НЕ возвращать chromadb.PersistentClient в горячий путь без проверки на segfault.
Проверка старта: `python C:/Temp/loader_test.py` → должно быть `LOADER_SURVIVED`.
