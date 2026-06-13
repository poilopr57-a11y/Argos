"""
skill_loader.py — Plugin-система навыков Аргоса
  Загружает навыки из директорий с manifest.yaml/json.
  Проверяет зависимости, версии, разрешения.
  Поддерживает версионирование и P2P-синхронизацию.
"""

import importlib, importlib.util, inspect, json, os, sys, traceback
from packaging.version import Version as V
from src.argos_logger import get_logger
from src.event_bus import get_bus, Events

log = get_logger("argos.skills")
bus = get_bus()

SKILLS_ROOT = "src/skills"

# Структура нового навыка
MANIFEST_SCHEMA = {
    "required": ["name", "version", "entry", "author"],
    "optional": [
        "description",
        "dependencies",
        "permissions",
        "min_argos_version",
        "tags",
        "category",
    ],
}

PERMISSIONS = {
    "network",  # доступ к сети
    "files",  # доступ к файлам
    "execute",  # выполнение команд
    "root",  # root-команды
    "iot",  # IoT устройства
    "telegram",  # Telegram уведомления
    "p2p",  # P2P сеть
}

PERMISSION_ALIASES = {
    "system": "execute",
    "file_write": "files",
}

ARGOS_VERSION = "2.1.3"

IMPORT_NAME_ALIASES = {
    "beautifulsoup4": "bs4",
    "pyyaml": "yaml",
    "python-telegram-bot": "telegram",
    "google-genai": "google.genai",
}


class SkillManifest:
    def __init__(self, data: dict, skill_dir: str):
        self.name = data.get("name", "unknown")
        self.version = data.get("version", "0.0.1")
        self.entry = data.get("entry", "skill.py")
        self.author = data.get("author", "unknown")
        self.description = data.get("description", "")
        self.dependencies = data.get("dependencies", [])
        raw_permissions = set(data.get("permissions", []))
        self.permissions = {PERMISSION_ALIASES.get(p, p) for p in raw_permissions}
        self.min_version = data.get("min_argos_version", "0.0.0")
        self.tags = data.get("tags", [])
        self.category = data.get("category", "general")
        self.dir = skill_dir
        self._raw = data

    def validate(self) -> list[str]:
        errors = []
        for f in MANIFEST_SCHEMA["required"]:
            if not getattr(self, f, None):
                errors.append(f"Отсутствует обязательное поле: {f}")
        invalid_perms = self.permissions - PERMISSIONS
        if invalid_perms:
            errors.append(f"Неизвестные разрешения: {invalid_perms}")
        try:
            V(self.version)
        except Exception:
            errors.append(f"Неверный формат версии: {self.version}")
        try:
            if V(ARGOS_VERSION) < V(self.min_version):
                errors.append(f"Требуется Аргос >= {self.min_version}, текущий {ARGOS_VERSION}")
        except Exception:
            pass
        return errors


class SkillInstance:
    def __init__(self, manifest: SkillManifest, module, runtime=None):
        self.manifest = manifest
        self.module = module
        self.runtime = runtime if runtime is not None else module
        self.loaded_at = __import__("time").time()
        self._started = False

    @staticmethod
    def _call_maybe_with_core(fn, core=None):
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):
            return fn()

        required = [
            p for p in sig.parameters.values()
            if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
            and p.default is inspect._empty
        ]
        if not required:
            return fn()
        return fn(core)

    def start(self, core=None):
        target = self.runtime
        log.info(
            "SkillInstance.start: module=%s runtime=%s core=%s",
            getattr(self.module, "__name__", "?"),
            type(target).__name__,
            type(core).__name__ if core else None,
        )
        if hasattr(target, "setup"):
            self._call_maybe_with_core(target.setup, core)
        elif hasattr(target, "start"):
            self._call_maybe_with_core(target.start, core)
        self._started = True

    def stop(self):
        target = self.runtime
        if hasattr(target, "teardown"):
            target.teardown()
        elif hasattr(target, "stop"):
            target.stop()
        self._started = False

    def handle(self, text: str, core=None):
        target = self.runtime
        if hasattr(target, "handle"):
            try:
                return target.handle(text, core)
            except TypeError:
                # Совместимость: часть legacy-скиллов ожидает handle(text).
                return target.handle(text)
        # Fallback: если runtime — экземпляр класса без handle(),
        # пробуем module-level handle() (паттерн: класс + модульный handle с проверкой триггеров)
        if self.module is not None and target is not self.module:
            module_handle = getattr(self.module, "handle", None)
            if callable(module_handle):
                try:
                    result = module_handle(text, core)
                except TypeError:
                    try:
                        result = module_handle(text)
                    except Exception:
                        result = None
                except Exception:
                    result = None
                if result is not None:
                    return result
        skill_name = (self.manifest.name or "").strip().lower()
        normalized_text = (text or "").strip().lower()
        if skill_name and skill_name in normalized_text:
            if hasattr(target, "execute"):
                return self._call_maybe_with_core(target.execute, core)
            if hasattr(target, "status"):
                return self._call_maybe_with_core(target.status, core)
            if hasattr(target, "report"):
                return self._call_maybe_with_core(target.report, core)
        return None

    @property
    def name(self):
        return self.manifest.name

    @property
    def version(self):
        return self.manifest.version


class SkillLoader:
    def __init__(self, core=None):
        self.core = core
        self._skills: dict[str, SkillInstance] = {}
        self._failed: dict[str, str] = {}
        self._import_pass = 0
        self._import_total = 0
        self._manifest_pass = 0
        self._manifest_total = 0

    def discover(self) -> list[str]:
        """Ищет все директории с manifest.json или manifest.yaml в SKILLS_ROOT."""
        found = []
        if not os.path.isdir(SKILLS_ROOT):
            return found
        for item in os.listdir(SKILLS_ROOT):
            skill_dir = os.path.join(SKILLS_ROOT, item)
            if os.path.isdir(skill_dir):
                for mf in ("manifest.json", "manifest.yaml", "manifest.yml"):
                    if os.path.exists(os.path.join(skill_dir, mf)):
                        found.append(item)
                        break
        return found

    def _load_manifest(self, skill_dir: str) -> SkillManifest | None:
        for mf in ("manifest.json", "manifest.yaml", "manifest.yml"):
            path = os.path.join(skill_dir, mf)
            if not os.path.exists(path):
                continue
            try:
                if mf.endswith(".json"):
                    data = json.load(open(path, encoding="utf-8"))
                else:
                    try:
                        import yaml

                        data = yaml.safe_load(open(path, encoding="utf-8"))
                    except ImportError:
                        # fallback: простой yaml parser
                        data = _simple_yaml_load(path)
                return SkillManifest(data, skill_dir)
            except Exception as e:
                log.error("Manifest load %s: %s", path, e)
        return None

    def _resolve_runtime(self, module, manifest: SkillManifest, core=None):
        register_fn = getattr(module, "register", None)
        if callable(register_fn):
            try:
                runtime = register_fn(core)
            except TypeError:
                runtime = register_fn()
            if runtime is not None:
                return runtime

        entry_class = ""
        if isinstance(getattr(manifest, "_raw", None), dict):
            entry_class = str(manifest._raw.get("entry_class", "")).strip()
        if entry_class and hasattr(module, entry_class):
            cls = getattr(module, entry_class)
            if inspect.isclass(cls):
                try:
                    return cls(core)
                except TypeError:
                    return cls()

        lifecycle_candidates = []
        public_candidates = []
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if cls.__module__ != getattr(module, "__name__", ""):
                continue
            if cls.__name__.startswith("_"):
                continue
            public_candidates.append(cls)
            if any(hasattr(cls, attr) for attr in ("handle", "start", "execute", "status", "report")):
                lifecycle_candidates.append(cls)

        selected = None
        if len(lifecycle_candidates) == 1:
            selected = lifecycle_candidates[0]
        elif len(public_candidates) == 1:
            selected = public_candidates[0]

        if selected is not None:
            try:
                return selected(core)
            except TypeError:
                return selected()

        return module

    def load(self, skill_name: str, core=None) -> str:
        skill_dir = os.path.join(SKILLS_ROOT, skill_name)
        if not os.path.isdir(skill_dir):
            # Fallback: старый стиль — один .py файл
            return self._load_legacy(skill_name, core)

        manifest = self._load_manifest(skill_dir)
        if not manifest:
            return f"❌ manifest не найден в {skill_dir}"

        errors = manifest.validate()
        if errors:
            return f"❌ Manifest ошибки: {'; '.join(errors)}"

        # Проверяем зависимости
        missing = self._check_deps(manifest.dependencies)
        if missing:
            return f"❌ Зависимости не установлены: {', '.join(missing)}\npip install {' '.join(missing)}"

        # Загружаем модуль
        entry_path = os.path.join(skill_dir, manifest.entry)
        if not os.path.exists(entry_path):
            return f"❌ Точка входа не найдена: {entry_path}"

        try:
            spec = importlib.util.spec_from_file_location(f"argos_skill_{skill_name}", entry_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            runtime = self._resolve_runtime(module, manifest, core)
            inst = SkillInstance(manifest, module, runtime=runtime)
            inst.start(core)
            self._skills[skill_name] = inst
            bus.emit(
                Events.SKILL_LOADED,
                {"name": skill_name, "version": manifest.version, "category": manifest.category},
                "skill_loader",
            )
            log.info("Навык загружен: %s v%s", skill_name, manifest.version)
            return f"✅ Навык '{manifest.name}' v{manifest.version} загружен."
        except Exception as e:
            self._failed[skill_name] = str(e)
            log.error("Навык %s ошибка: %s", skill_name, e)
            return f"❌ {skill_name}: {e}"

    def _load_legacy(self, skill_name: str, core=None) -> str:
        """Загружает старый формат: src/skills/skill_name.py"""
        try:
            mod = importlib.import_module(f"src.skills.{skill_name}")
            manifest = SkillManifest(
                {
                    "name": skill_name,
                    "version": "0.1.0",
                    "entry": f"{skill_name}.py",
                    "author": "legacy",
                },
                os.path.join(SKILLS_ROOT, skill_name),
            )
            runtime = self._resolve_runtime(mod, manifest, core)
            inst = SkillInstance(manifest, mod, runtime=runtime)
            inst.start(core)  # Call setup() with core
            self._skills[skill_name] = inst
            return f"✅ Навык '{skill_name}' загружен (legacy mode)."
        except Exception as e:
            return f"❌ {skill_name}: {e}"

    def _check_deps(self, deps: list) -> list:
        missing = []
        for dep in deps:
            pkg = dep.split(">=")[0].split("==")[0].strip()
            module_name = IMPORT_NAME_ALIASES.get(pkg.lower(), pkg.replace("-", "_"))
            try:
                importlib.import_module(module_name)
            except ImportError:
                missing.append(dep)
        return missing

    def unload(self, skill_name: str) -> str:
        inst = self._skills.get(skill_name)
        if not inst:
            return f"❌ Навык '{skill_name}' не загружен."
        inst.stop()
        del self._skills[skill_name]
        return f"✅ Навык '{skill_name}' выгружен."

    def reload(self, skill_name: str, core=None) -> str:
        self.unload(skill_name)
        return self.load(skill_name, core)

    def dispatch(self, text: str, core=None) -> str | None:
        for name, inst in self._skills.items():
            try:
                log.info("Dispatch checking skill: %s", name)
                result = inst.handle(text, core)
                if result:
                    log.info("Skill %s matched, result length: %d", name, len(result) if result else 0)
                    bus.emit(
                        Events.SKILL_EXECUTED, {"skill": name, "input": text[:80]}, "skill_loader"
                    )
                    return result
            except Exception as e:
                log.error("Skill %s dispatch error: %s", name, e)
        return None

    def load_all(self, core=None) -> str:
        names = self.discover()
        self._manifest_total = len(names)
        self._manifest_pass = 0
        self._import_pass = 0
        self._import_total = 0

        results = [f"📦 Обнаружено manifest-навыков: {len(names)}"]
        for name in names:
            msg = self.load(name, core)
            if msg.startswith("✅"):
                self._manifest_pass += 1
            results.append(msg)

        imported_line = self.import_all_skills(core=core)
        results.append(imported_line)
        results.append(
            f"SkillLoader load_all (manifest навыки) → PASS {self._manifest_pass}/{self._manifest_total}"
        )
        return "\n".join(results)

    def import_all_skills(self, core=None) -> str:
        """
        Автоматически импортирует ВСЕ flat-скиллы из src/skills/*.py.
        Не дублирует уже загруженные manifest-навыки.
        """
        skills_dir = os.path.join(SKILLS_ROOT)
        if not os.path.isdir(skills_dir):
            self._import_total = 0
            self._import_pass = 0
            return "Импорт всех skills (src/skills) → PASS 0/0"

        module_files = sorted(
            f
            for f in os.listdir(skills_dir)
            if f.endswith(".py") and not f.startswith("_") and f != "__init__.py"
        )
        self._import_total = len(module_files)
        self._import_pass = 0
        details = []

        for filename in module_files:
            skill_name = os.path.splitext(filename)[0]

            # Уже загружен (например manifest-пакетом)
            if skill_name in self._skills:
                self._import_pass += 1
                details.append(f"✅ {skill_name} (already loaded)")
                continue

            try:
                mod = importlib.import_module(f"src.skills.{skill_name}")
                # Читаем описание/версию из атрибутов модуля если есть
                _desc    = getattr(mod, "SKILL_DESCRIPTION", "") or getattr(mod, "__description__", "")
                _version = getattr(mod, "SKILL_VERSION",     "") or getattr(mod, "__version__", "0.1.0")
                manifest = SkillManifest(
                    {
                        "name":        getattr(mod, "SKILL_NAME", skill_name),
                        "version":     _version,
                        "entry":       filename,
                        "author":      getattr(mod, "SKILL_AUTHOR", "legacy"),
                        "description": _desc,
                    },
                    os.path.join(SKILLS_ROOT, skill_name),
                )
                runtime = self._resolve_runtime(mod, manifest, core)
                inst = SkillInstance(manifest, mod, runtime=runtime)
                inst.start(core)
                self._skills[skill_name] = inst
                self._import_pass += 1
                details.append(f"✅ {skill_name}")
            except Exception as e:
                self._failed[skill_name] = str(e)
                details.append(f"❌ {skill_name}: {e}")

        summary = f"Импорт всех skills (src/skills) → PASS {self._import_pass}/{self._import_total}"
        return "\n".join([summary, *details])

    def list_skills(self) -> str:
        loaded = list(self._skills.values())
        failed = self._failed
        lines = [f"🧩 НАВЫКИ АРГОСА ({len(loaded)} загружено):"]
        if self._import_total:
            lines.append(f"  • Импорт всех skills (src/skills): {self._import_pass}/{self._import_total}")
        if self._manifest_total:
            lines.append(
                f"  • SkillLoader load_all (manifest): {self._manifest_pass}/{self._manifest_total}"
            )
        by_cat = {}
        for inst in loaded:
            cat = inst.manifest.category
            by_cat.setdefault(cat, []).append(inst)
        for cat, skills in sorted(by_cat.items()):
            lines.append(f"\n  [{cat.upper()}]")
            for s in skills:
                desc = s.manifest.description[:50]
                lines.append(f"    ✅ {s.name} v{s.version} — {desc}")
        if failed:
            lines.append(f"\n  [ОШИБКИ]")
            for name, err in failed.items():
                lines.append(f"    ❌ {name}: {err[:60]}")
        return "\n".join(lines) if len(lines) > 1 else "Навыков нет."

    def smoke_check_all(self, core=None) -> str:
        """
        Проверка фактического запуска каждого flat-скилла:
        import -> setup -> handle.
        """
        skills_dir = os.path.join(SKILLS_ROOT)
        if not os.path.isdir(skills_dir):
            return "❌ src/skills не найден."

        module_files = sorted(
            f
            for f in os.listdir(skills_dir)
            if f.endswith(".py") and not f.startswith("_") and f != "__init__.py"
        )
        total = len(module_files)
        passed = 0
        lines = [f"🧪 SKILLS CHECK ALL: total={total}"]

        for filename in module_files:
            skill_name = os.path.splitext(filename)[0]
            ok_import = ok_setup = ok_handle = False
            err = ""
            try:
                mod = importlib.import_module(f"src.skills.{skill_name}")
                ok_import = True

                if hasattr(mod, "setup"):
                    try:
                        mod.setup(core)
                    except TypeError:
                        mod.setup()
                    ok_setup = True
                else:
                    ok_setup = True

                if hasattr(mod, "handle"):
                    try:
                        _ = mod.handle("status", core)
                    except TypeError:
                        _ = mod.handle("status")
                    ok_handle = True
                else:
                    ok_handle = True

            except Exception as e:
                err = str(e)
                if not err:
                    err = traceback.format_exc(limit=1).strip()

            if ok_import and ok_setup and ok_handle:
                passed += 1
                lines.append(f"✅ {skill_name} | import=ok setup=ok handle=ok")
            else:
                lines.append(
                    f"❌ {skill_name} | import={ok_import} setup={ok_setup} handle={ok_handle} | {err}"
                )

        lines.append(f"ИТОГ: PASS {passed}/{total}")
        return "\n".join(lines)

    def create_skill_template(self, name: str, category: str = "custom") -> str:
        skill_dir = os.path.join(SKILLS_ROOT, name)
        os.makedirs(skill_dir, exist_ok=True)

        manifest = {
            "name": name,
            "version": "1.0.0",
            "entry": "skill.py",
            "author": "Всеволод",
            "description": f"Навык {name}",
            "category": category,
            "tags": [category],
            "dependencies": [],
            "permissions": ["network"],
        }
        json.dump(
            manifest,
            open(os.path.join(skill_dir, "manifest.json"), "w", encoding="utf-8"),
            indent=2,
            ensure_ascii=False,
        )

        skill_code = f'''"""
{name} — Навык Аргоса
Автогенерировано ArgosUniversal
"""

TRIGGERS = ["{name.lower()}", "{name.lower().replace("_"," ")}"]

def setup(core=None):
    """Инициализация навыка."""
    pass

def handle(text: str, core=None) -> str | None:
    """Обработка команды. Вернуть None если не наш запрос."""
    t = text.lower()
    if not any(tr in t for tr in TRIGGERS):
        return None
    return f"✅ Навык {name}: обработка {{text[:50]}}"

def teardown():
    """Завершение работы навыка."""
    pass
'''
        with open(os.path.join(skill_dir, "skill.py"), "w", encoding="utf-8") as f:
            f.write(skill_code)
        with open(os.path.join(skill_dir, "README.md"), "w", encoding="utf-8") as f:
            f.write(f"# {name}\n\nКатегория: {category}\n\n## Команды\n\n- `{name}` — ...\n")

        return (
            f"✅ Шаблон навыка создан: {skill_dir}/\n"
            f"   📄 skill.py — логика\n"
            f"   📄 manifest.json — декларация\n"
            f"   📄 README.md — документация"
        )


def _simple_yaml_load(path: str) -> dict:
    """Минимальный YAML-парсер (только простые key: value)."""
    result = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" in line:
                k, _, v = line.partition(":")
                v = v.strip().strip("\"'")
                if v.startswith("["):
                    result[k.strip()] = [
                        x.strip().strip("\"'") for x in v.strip("[]").split(",") if x.strip()
                    ]
                else:
                    result[k.strip()] = v
    return result
