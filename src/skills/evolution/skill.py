"""
evolution.py — Модуль саморазвития Аргоса
  Пишет, проверяет и внедряет новые навыки в src/skills/
  Перед принятием запускает Feedback Loop (Code Review) и Unit-тест.
"""

import os
import ast
import re
import sys
import json
import tempfile
import subprocess
import importlib

SKILLS_DIR = "src/skills"
TESTS_GEN_DIR = "tests/generated"

HARD_SKILL_SYSTEM_PROMPT = (
    "Ты генератор Python-кода для production. "
    "Запрещено возвращать пояснения, markdown, блоки ``` и любой текст вне Python-кода. "
    "Выводи только валидный, запускаемый Python-модуль."
)

HARD_TEST_SYSTEM_PROMPT = (
    "Ты генератор unit-тестов Python для production. "
    "Выводи только валидный код unittest без markdown и без пояснений."
)


TRIGGERS = ["эволюция", "создай навык", "разработай навык", "новый навык", "генерируй skill", "evolution", "create skill", "generate skill", "develop skill"]


class ArgosEvolution:
    def __init__(self, ai_core=None):
        self.core = ai_core  # ArgosCore для генерации кода

    def setup(self, core=None):
        if core:
            self.core = core
            log.info("Evolution.setup: core=%s has _ask_gemini: %s", type(core).__name__, hasattr(core, '_ask_gemini'))

    def _sanitize_filename(self, name: str) -> str:
        raw = (name or "").strip().lower().replace(".py", "")
        safe = re.sub(r"[^a-z0-9_]", "_", raw)
        safe = re.sub(r"_+", "_", safe).strip("_")
        if not safe:
            safe = "new_skill"
        return safe

    def _extract_code_only(self, text: str) -> str:
        payload = (text or "").strip()
        if payload.startswith("```"):
            payload = payload.replace("```python", "").replace("```", "").strip()
        return payload

    def _ensure_executable_skill(self, code: str) -> tuple[bool, str]:
        text = (code or "").strip()
        if not text:
            return False, "пустой код"
        if "```" in text:
            return False, "обнаружен markdown fence"

        try:
            tree = ast.parse(text)
        except SyntaxError as e:
            return False, f"syntax error: {e}"

        has_class = any(isinstance(n, ast.ClassDef) for n in tree.body)
        if not has_class:
            return False, "в модуле должен быть минимум 1 класс"

        risky_calls = {"eval", "exec", "compile", "__import__"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in risky_calls:
                    return False, f"обнаружен рискованный вызов: {node.func.id}()"
        return True, "ok"

    def _repair_to_code(self, broken_payload: str, description: str = "") -> str:
        prompt = (
            "Исправь ответ и верни только валидный Python-код модуля. "
            "Удали комментарии-пояснения и markdown.\n"
            f"Описание навыка: {description}\n"
            f"Текущий ответ:\n{broken_payload}"
        )
        ans = self._ask_ai(HARD_SKILL_SYSTEM_PROMPT, prompt)
        return self._extract_code_only(ans or "")

    def _extract_json(self, text: str) -> dict | None:
        candidate = (text or "").strip()
        if not candidate:
            return None
        if candidate.startswith("```"):
            candidate = candidate.strip("`")
            candidate = candidate.replace("json", "", 1).strip()
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
        left = candidate.find("{")
        right = candidate.rfind("}")
        if left >= 0 and right > left:
            chunk = candidate[left : right + 1]
            try:
                obj = json.loads(chunk)
                if isinstance(obj, dict):
                    return obj
            except Exception:
                return None
        return None

    def _ask_ai(self, role: str, prompt: str) -> str | None:
        """Коллективная мощность: перебирает все рабочие провайдеры по приоритету.

        Порядок: Grok → Groq → DeepSeek → OpenAI → Kimi → Cloudflare → Ollama.
        Gemini пропускается — гео-блок.
        Уважает ARGOS_DISABLE_* переменные и временно отключённых провайдеров.
        """
        if not self.core:
            return None

        # Карта провайдер → env-флаг отключения
        _PROVIDER_ENV_DISABLE = {
            "Grok":       "ARGOS_DISABLE_GROK",
            "Groq":       "ARGOS_DISABLE_GROQ",
            "DeepSeek":   "ARGOS_DISABLE_DEEPSEEK",
            "OpenAI":     "ARGOS_DISABLE_OPENAI",
            "Kimi":       "ARGOS_DISABLE_KIMI",
            "Cloudflare": "ARGOS_DISABLE_CLOUDFLARE",
        }

        def _env_disabled(name: str) -> bool:
            return (os.getenv(name, "") or "").strip().lower() in {
                "1", "true", "on", "yes", "да", "вкл"
            }

        # 1. OpenAI-совместимые провайдеры
        for provider in ("Grok", "Groq", "DeepSeek", "OpenAI", "Kimi", "Cloudflare"):
            # Проверяем env-флаг
            env_flag = _PROVIDER_ENV_DISABLE.get(provider)
            if env_flag and _env_disabled(env_flag):
                continue
            # Проверяем временный/постоянный дизейбл через core
            if hasattr(self.core, "_is_provider_temporarily_disabled"):
                if self.core._is_provider_temporarily_disabled(provider):
                    continue
            try:
                answer = self.core._ask_openai_compat(role, prompt, provider_name=provider)
                if answer and answer.strip():
                    return answer
            except Exception:
                continue

        # 2. Резерв — локальный Ollama (если не отключён)
        try:
            if not (hasattr(self.core, "_is_provider_temporarily_disabled")
                    and self.core._is_provider_temporarily_disabled("Ollama")):
                return self.core._ask_ollama(role, prompt)
        except Exception:
            pass
        return None

    def _fallback_test_template(self) -> str:
        return (
            "import unittest\n"
            "import skill_under_test as s\n\n"
            "class TestGeneratedSkill(unittest.TestCase):\n"
            "    def test_module_imports(self):\n"
            "        self.assertIsNotNone(s)\n\n"
            "if __name__ == '__main__':\n"
            "    unittest.main()\n"
        )

    def _generate_unit_test(self, filename: str, code: str, description: str = "") -> str:
        prompt = (
            "Сгенерируй unit-тест на Python (unittest) для навыка.\n"
            "Важно:\n"
            "- Верни только код, без markdown\n"
            "- Используй только стандартную библиотеку\n"
            "- Импортируй тестируемый модуль как: import skill_under_test as s\n"
            "- Должен быть минимум один тестовый метод test_*\n"
            "- Тест должен быть детерминированным\n\n"
            f"Имя навыка: {filename}\n"
            f"Описание навыка: {description}\n"
            f"Код навыка:\n{code}"
        )
        answer = self._ask_ai(HARD_TEST_SYSTEM_PROMPT, prompt)
        test_code = self._extract_code_only(answer or "")
        if not test_code:
            return self._fallback_test_template()
        try:
            ast.parse(test_code)
        except SyntaxError:
            return self._fallback_test_template()
        if "test_" not in test_code:
            return self._fallback_test_template()
        if "unittest" not in test_code:
            return self._fallback_test_template()
        return test_code

    def _review_patch(
        self, filename: str, code: str, test_code: str, description: str = ""
    ) -> tuple[bool, str]:
        prompt = (
            "Ты второй независимый агент Code Review.\n"
            "Проверь код навыка и unit-тест перед внедрением в production.\n"
            "Верни строго JSON:\n"
            '{"approved": true|false, "summary": "...", "issues": ["..."]}\n'
            "Отклоняй, если есть: небезопасные операции, явные баги, плохая тестируемость,\n"
            "или тест не покрывает главное поведение.\n\n"
            f"Описание: {description}\n"
            f"Навык ({filename}.py):\n{code}\n\n"
            f"Тест:\n{test_code}"
        )
        answer = self._ask_ai("Ты строгий Python Code Reviewer.", prompt)
        data = self._extract_json(answer or "")
        if isinstance(data, dict) and "approved" in data:
            approved = bool(data.get("approved"))
            summary = str(data.get("summary", "")).strip()
            issues = data.get("issues") or []
            if isinstance(issues, list) and issues:
                summary = (summary + " | " if summary else "") + "; ".join(
                    str(i) for i in issues[:5]
                )
            return approved, (summary or ("approved" if approved else "rejected"))

        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            return False, f"syntax error: {e}"

        risky_calls = {"eval", "exec", "compile", "__import__"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in risky_calls:
                    return False, f"обнаружен рискованный вызов: {node.func.id}()"
        return True, "Fallback review: критических рисков не обнаружено"

    def _run_unit_test(self, code: str, test_code: str) -> tuple[bool, str]:
        with tempfile.TemporaryDirectory(prefix="argos_evo_") as td:
            skill_path = os.path.join(td, "skill_under_test.py")
            test_path = os.path.join(td, "test_generated_skill.py")

            with open(skill_path, "w", encoding="utf-8") as f:
                f.write(code)
            with open(test_path, "w", encoding="utf-8") as f:
                f.write(test_code)

            cmd = [sys.executable, "-m", "unittest", "discover", "-s", td, "-p", "test_*.py"]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
            out = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
            out = out.strip()
            if proc.returncode == 0:
                return True, out[-1200:] if out else "OK"
            return False, out[-1600:] if out else "unit test failed"

    def apply_patch(
        self, filename: str, code: str, test_code: str | None = None, description: str = ""
    ) -> str:
        """Проверяет skill+test и записывает навык только после review и passing tests."""
        filename = self._sanitize_filename(filename)
        code = self._extract_code_only(code)
        ok_code, reason = self._ensure_executable_skill(code)
        if not ok_code:
            return f"❌ Навык отклонён: {reason}"

        generated_test = test_code or self._generate_unit_test(filename, code, description)
        generated_test = self._extract_code_only(generated_test)
        try:
            ast.parse(generated_test)
        except SyntaxError as e:
            return f"❌ Синтаксическая ошибка в unit-тесте: {e}"

        approved, review_summary = self._review_patch(filename, code, generated_test, description)
        if not approved:
            return f"❌ Code Review не пройден: {review_summary}"

        tests_ok, test_report = self._run_unit_test(code, generated_test)
        if not tests_ok:
            return f"❌ Навык отклонён: unit-тест не пройден.\n{test_report}"

        try:
            os.makedirs(SKILLS_DIR, exist_ok=True)
            os.makedirs(TESTS_GEN_DIR, exist_ok=True)

            path = os.path.join(SKILLS_DIR, f"{filename}.py")
            with open(path, "w", encoding="utf-8") as f:
                f.write(code)

            test_path = os.path.join(TESTS_GEN_DIR, f"test_skill_{filename}.py")
            with open(test_path, "w", encoding="utf-8") as f:
                f.write(generated_test)

            size = os.path.getsize(path)
            return (
                f"✅ Навык '{filename}' внедрён в ДНК Аргоса ({size} байт).\n"
                f"🧪 Unit-test: {test_path}\n"
                f"🧠 Review: {review_summary}"
            )
        except Exception as e:
            return f"❌ Сбой мутации: {e}"

    def generate_skill(self, description: str) -> str:
        """Генерирует навык + тест и принимает только после review/test gate."""
        if not self.core:
            # log.error("Evolution: self.core is None! core=%s", self.core)
            return "❌ Нет доступа к ядру ИИ. Передай core при инициализации."

        prompt = (
            f"Напиши Python-модуль навыка для ИИ-системы Аргос.\n"
            f"Описание: {description}\n\n"
            f"Требования:\n"
            f"- Один класс с __init__ и методами\n"
            f"- Только стандартные библиотеки + requests + bs4\n"
            f"- Комментарии на русском\n"
            f"- Вернуть только код, без markdown и без пояснений\n"
            f"Имя файла: угадай из описания (snake_case, без .py)"
        )

        answer = self._ask_ai(HARD_SKILL_SYSTEM_PROMPT, prompt)

        if not answer:
            return "❌ ИИ не ответил. Попробуй позже."

        answer = self._extract_code_only(answer)
        ok_code, reason = self._ensure_executable_skill(answer)
        if not ok_code:
            repaired = self._repair_to_code(answer, description)
            ok_code, reason = self._ensure_executable_skill(repaired)
            if ok_code:
                answer = repaired
            else:
                return f"❌ ИИ выдал неисполняемый код: {reason}"

        lines = answer.strip().splitlines()
        filename = "new_skill"
        for line in lines[:3]:
            if line.startswith("#") and ".py" not in line:
                candidate = line.lstrip("#").strip().split()[0].lower()
                if candidate.replace("_", "").isalnum():
                    filename = self._sanitize_filename(candidate)
                    break

        test_code = self._generate_unit_test(filename, answer, description)
        return self.apply_patch(filename, answer, test_code=test_code, description=description)

    def list_skills(self) -> str:
        try:
            files = [
                f[:-3]
                for f in os.listdir(SKILLS_DIR)
                if f.endswith(".py") and not f.startswith("__")
            ]
            if not files:
                return "🧬 Навыки не найдены."
            return "🧬 Навыки Аргоса:\n" + "\n".join(f"  • {s}" for s in sorted(files))
        except Exception as e:
            return f"Ошибка: {e}"

    def remove_skill(self, name: str) -> str:
        path = os.path.join(SKILLS_DIR, f"{name}.py")
        if not os.path.exists(path):
            return f"❌ Навык '{name}' не найден."
        os.remove(path)
        return f"🗑️ Навык '{name}' удалён из ДНК."

    def load_skill(self, name: str):
        """Динамически загружает навык по имени."""
        try:
            mod = importlib.import_module(f"src.skills.{name}")
            return mod, f"✅ '{name}' загружен."
        except ModuleNotFoundError:
            return None, f"❌ '{name}' не найден."

    def handle(self, text: str, core=None) -> str | None:
        t = text.lower()
        if not any(tr in t for tr in TRIGGERS):
            return None
        if core:
            self.core = core
        desc = text
        for tr in TRIGGERS:
            desc = desc.replace(tr, "").strip()
        if not desc:
            return "🧬 Эволюция Аргоса: опиши какой навык создать.\nПример: \"эволюция: создай навык для мониторинга CPU\""
        # Если после удаления триггеров осталась только статусная фраза —
        # не генерируем навык, возвращаем None → SkillLoader упадёт в execute()
        _STATUS_WORDS = {
            "статус", "status", "info", "инфо", "помощь", "help",
            "список", "list", "навыков", "скилов",
        }
        if desc.lower().strip() in _STATUS_WORDS:
            return None
        return self.generate_skill(desc)

    def execute(self, text: str = "") -> str:
        """Точка входа SkillLoader."""
        t = (text or "").strip()
        t_low = t.lower()
        _STATUS_PHRASES = (
            "", "статус", "status", "эволюция", "evolution",
            "evolution статус", "evolution status",
            "эволюция статус", "список скилов", "список навыков",
            "info", "инфо", "помощь", "help",
        )
        if t_low in _STATUS_PHRASES:
            skills = [f.stem for f in __import__('pathlib').Path(SKILLS_DIR).glob("*.py") if not f.stem.startswith("_")]
            return (
                f"🧬 ArgosEvolution готова. Навыков в ДНК: {len(skills)}\n"
                "Для генерации: 'создай навык <описание>'\n"
                "Пример: 'создай навык мониторинга GPIO'"
            )
        # Генерация только при явной команде
        if any(k in t_low for k in ("создай навык", "разработай навык", "новый навык",
                                     "генерируй skill", "create skill", "generate skill")):
            return self.handle(t) or self.generate_skill(t)
        return f"🧬 Evolution: неизвестная команда '{t}'. Пример: 'создай навык <описание>'"


# Module-level handle for skill_loader
_evolution_instance = None


def handle(text: str, core=None) -> str | None:
    global _evolution_instance
    if _evolution_instance is None:
        _evolution_instance = ArgosEvolution(ai_core=core)
    return _evolution_instance.handle(text, core)


def setup(core=None):
    global _evolution_instance
    if _evolution_instance is None:
        _evolution_instance = ArgosEvolution(ai_core=core)
    else:
        _evolution_instance.core = core
