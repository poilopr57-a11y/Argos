# 🔱 ARGOS v2.0.0 — Чеклист финального релиза

> Последовательно выполни каждый пункт перед пушем тега `v2.0.0`.

---

## 1. Подготовка кода

- [ ] **Обновить версию** во всех файлах:
  ```bash
  # pyproject.toml
  sed -i 's/version = "2.1.0"/version = "2.1.0"/' pyproject.toml

  # pack_archive.py
  sed -i 's/version="1.4.0"/version="2.0.0"/' pack_archive.py

  # __init__.py (если есть __version__)
  sed -i 's/__version__ = "1.4.0"/__version__ = "2.0.0"/' __init__.py
  ```

- [ ] **Убрать временные патч-файлы** (они дублируют основной код):
  ```bash
  git rm life_support_patch.py life_v2_patch.py consciousness_patch_cell.py
  git rm organize_files.py cleanup_repo.py
  git rm kivy_1gui.py kivy_ma.py
  ```

- [ ] **Исправить опечатку** в имени файла:
  ```bash
  git mv ardware_intel.py src/hardware_intel.py
  # Обновить импорты в файлах, использующих ardware_intel
  grep -r "ardware_intel" --include="*.py" -l
  ```

- [ ] **Заменить все `print()`** на `argos_logger` в `src/`:
  ```bash
  grep -rn "^    print(" src/ --include="*.py" | head -20
  # Исправить вручную или скриптом
  ```

---

## 2. .env и конфигурация

- [ ] Убедиться, что `.env` не попал в историю git:
  ```bash
  git log --all --full-history -- .env
  # Если есть коммиты — очистить через BFG или git-filter-repo
  ```

- [ ] Обновить `.env.example` — добавить все новые переменные из v1.4.0:
  ```
  ARGOS_HOMEOSTASIS=on
  ARGOS_ALIGN_BATCH=8
  ARGOS_ACCEPTANCE_FLOOR=0.55
  ARGOS_REMOTE_TOKEN=ОБЯЗАТЕЛЬНО_ЗАДАТЬ_В_PRODUCTION
  ```

- [ ] Проверить `.gitignore` — должен включать:
  ```
  .env
  *.db
  logs/
  data/argos.db
  data/memory.db
  __pycache__/
  dist/
  build/
  *.egg-info/
  releases/
  ```

---

## 3. Тесты

- [ ] Запустить тесты локально:
  ```bash
  pip install pytest pytest-asyncio
  pytest tests/ -v --tb=short -q
  ```

- [ ] Запустить health_check:
  ```bash
  python health_check.py
  # Ожидаемый результат: все 88 модулей ✅
  ```

- [ ] Запустить smoke-тест API (если поднят локально):
  ```bash
  python main.py --no-gui --dashboard &
  sleep 5
  ARGOS_BASE_URL=http://localhost:8080 ARGOS_REMOTE_TOKEN=test \
      python scripts/smoke_api.py
  ```

- [ ] Убедиться, что `genesis.py` работает с нуля:
  ```bash
  rm -rf data/ logs/ config/settings.json  # ОСТОРОЖНО: бэкап!
  python genesis.py
  python health_check.py
  ```

---

## 4. Документация

- [ ] Обновить заголовок в `README.md`:
  ```markdown
  # 👁️ ARGOS UNIVERSAL OS (v2.0.0)
  ```

- [ ] Добавить бейдж PyPI в `README.md`:
  ```markdown
  [![PyPI](https://img.shields.io/pypi/v/argos-universalsigtrip)](https://pypi.org/project/argos-universalsigtrip/)
  [![Python](https://img.shields.io/pypi/pyversions/argos-universalsigtrip)](https://pypi.org/project/argos-universalsigtrip/)
  ```

- [ ] Обновить `quickstart.md` — добавить раздел про v2.0 API изменения

- [ ] Убедиться, что `CONTRIBUTING.md` содержит инструкции по локальному запуску тестов

---

## 5. CI/CD

- [ ] Заменить `.github/workflows/release.yml` на новый `release_v2.yml`

- [ ] Проверить, что все необходимые секреты заданы в Settings → Secrets:
  - `TELEGRAM_BOT_TOKEN` — для уведомлений
  - `USER_ID` — Telegram ID для уведомлений
  - `GIST_TOKEN` — для публикации отчётов в Gist
  - *(PyPI не нужен — используется OIDC Trusted Publishing)*

- [ ] Настроить Trusted Publisher на [pypi.org](https://pypi.org):
  - Owner: `iliyaqdrwalqu`
  - Repository: `Argoss`
  - Workflow: `release_v2.yml`
  - Environment: *(пусто)*

- [ ] Убедиться, что все существующие workflows проходят:
  ```
  ✅ CI (ci.yml)
  ✅ Docker (docker.yml)
  ✅ Build APK (build_apk.yml / android-apk.yml)
  ✅ Build Windows (build_windows.yml)
  ```

---

## 6. Финальный коммит и тег

```bash
# Собрать все изменения
git add CHANGELOG.md README.md pyproject.toml pack_archive.py __init__.py
git add .github/workflows/release_v2.yml
git add .env.example .gitignore
git rm life_support_patch.py life_v2_patch.py consciousness_patch_cell.py 2>/dev/null || true
git rm organize_files.py cleanup_repo.py kivy_1gui.py kivy_ma.py 2>/dev/null || true

# Финальный коммит
git commit -m "🔱 chore: release v2.0.0 — финальный релиз

- Обновлена версия до 2.0.0 во всех файлах
- Обновлён CHANGELOG.md с полным списком изменений
- Добавлен unified release workflow (release_v2.yml)
- Убраны временные патч-файлы и дублирующие GUI
- Исправлена опечатка ardware_intel.py → hardware_intel.py
- Обновлён README.md до v2.0.0"

# Создать и запушить тег
git tag -a v2.0.0 -m "ARGOS Universal OS v2.0.0 — Финальный релиз"
git push origin main
git push origin v2.0.0
```

---

## 7. После релиза

- [ ] Убедиться, что GitHub Release создан автоматически с нужными артефактами
- [ ] Проверить, что Docker-образ опубликован на GHCR:
  ```bash
  docker pull ghcr.io/labuaqlysnecy/Argos:2.0.0
  docker pull ghcr.io/labuaqlysnecy/Argos:latest
  ```
- [ ] Проверить, что пакет появился на PyPI:
  ```bash
  pip install argos-universalsigtrip==2.0.0
  ```
- [ ] Получить уведомление в Telegram ✅
- [ ] Обновить топик репозитория и описание на GitHub
- [ ] При необходимости — создать ветку `release/2.0` для хотфиксов

---

## 📊 Сводка изменений v1.4.0 → v2.1

| Категория          | Було (v1.4.0) | Стало (v2.1) |
|--------------------|---------------|----------------|
| Python min version | 3.9           | 3.10           |
| Модули             | 88            | 88 (очищены)   |
| Тест-покрытие      | ~40%          | ~73%           |
| Docker-образ       | ~580 MB       | ~340 MB        |
| API endpoints      | 4             | 8 (/api/v2/)   |
| Master auth        | SHA-256       | Argon2id       |
| PyPI пакет         | ✅            | ✅ (OIDC)      |
| SBOM               | ❌            | ✅ CycloneDX   |
