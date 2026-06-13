"""
arc_agi3_skill.py — ARGOS ↔ ARC-AGI-3 (делегирует в arc_play.py)

arc_play.py запускает игру в изолированном .venv_arc, где установлен
настоящий arc-agi с Arcade + arcengine. ARC_API_KEY читается из .env.

Почему делегирование, а не прямой import:
  • В системном Python установлен arc-agi v0.0.7 (только датасеты ARC1/2)
  • В .venv_arc — игровой arc-agi (Arcade, make, step, scorecard)
  • arc_play.py запускает .venv_arc/python как subprocess → правильный пакет

ARC_API_KEY → three.arcprize.org/api/games → скачивает среду → arcengine
Без ключа: анонимный ключ через /api/games/anonkey (ограниченный доступ)

Команды:
  arc статус          — venv, ключ API, текущий/последний запуск
  arc среды           — окружения из policy + дефолтные
  arc решай <env_id>  — запустить среду (делегирует arc_play.start_game_async)
  arc решай <env> <N> — запустить с N шагов
  arc авто            — автовыбор среды/действия (epsilon-greedy)
  arc история         — статистика + QML-рекомендации
  arc стоп            — статус остановки (subprocess управляется arc_play)
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path
from typing import Optional
from src.argos_logger import get_logger

log = get_logger("argos.arc3")

# ── Импорт arc_play из корня проекта ─────────────────────────────────────────
# Корень: src/skills/ → ../.. → src/ → ../../.. → project root
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

_ARC_PLAY_OK = False
arc_play = None  # type: ignore[assignment]

try:
    if str(_PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(_PROJECT_ROOT))
    import arc_play as _arc_play_module
    arc_play = _arc_play_module
    _ARC_PLAY_OK = True
except Exception as _import_err:
    log.warning("[ARC3] arc_play.py недоступен: %s", _import_err)

# ── Константы ─────────────────────────────────────────────────────────────────
ARC3_API_KEY_ENV = "ARC_API_KEY"
ARC3_API_BASE    = "https://three.arcprize.org"
ARC3_DEFAULT_ENVS = ["ls20", "ft09", "tr28"]


class ARC3Agent:
    """
    Агент ARC-AGI-3.

    Вся игровая логика выполняется в arc_play.py через .venv_arc subprocess.
    Этот класс отвечает за:
      — форматирование команд и ответов для пользователя
      — LLM-анализ результатов из arc_history.jsonl
      — маршрутизацию handle() → arc_play.*
    """

    def __init__(self, core=None):
        self.core = core

    # ── Внутренние утилиты ────────────────────────────────────────────────────

    def _key_line(self) -> str:
        key = os.getenv(ARC3_API_KEY_ENV, "").strip()
        if key:
            return f"✅ {ARC3_API_KEY_ENV} задан (three.arcprize.org)"
        return f"⚠️ {ARC3_API_KEY_ENV} не задан → работает анонимно"

    def _require_arc_play(self) -> Optional[str]:
        """Возвращает сообщение об ошибке если arc_play недоступен, иначе None."""
        if not _ARC_PLAY_OK:
            return (
                "❌ arc_play.py не найден в корне проекта.\n"
                f"   Ожидается: {_PROJECT_ROOT / 'arc_play.py'}"
            )
        return None

    # ── Команды ───────────────────────────────────────────────────────────────

    def status(self) -> str:
        lines = ["🎮 ARC-AGI-3 (arc_play.py + .venv_arc):"]

        err = self._require_arc_play()
        if err:
            lines.append(f"  {err}")
            return "\n".join(lines)

        # Состояние venv
        try:
            venv_ok, venv_msg = arc_play.ensure_arc_venv()
            lines.append(f"  {'✅' if venv_ok else '❌'} venv: {venv_msg}")
        except Exception as e:
            lines.append(f"  ⚠️ venv: проверка не удалась ({e})")

        # API-ключ
        lines.append(f"  {self._key_line()}")

        # Статус текущего/последнего запуска
        try:
            st = arc_play.get_status()
            state = st.get("state", "idle")
            if state == "running":
                lines.append(
                    f"  🔄 Запущено: {st.get('env_id', '?')} | "
                    f"шагов: {st.get('steps', '?')} | "
                    f"действие: {st.get('action_name', '?')}"
                )
            elif state == "done":
                sc = st.get("scorecard", {})
                score = st.get("score", sc.get("score", "?"))
                actions = st.get("total_actions", sc.get("total_actions", "?"))
                lines.append(
                    f"  ✅ Завершено: {st.get('env_id', '?')} | "
                    f"score={score} | действий={actions}"
                )
            elif state == "error":
                lines.append(f"  ❌ Ошибка: {st.get('message', '?')}")
            elif state == "stale":
                lines.append("  ⚠️ Зависший запуск (процесс завершился без финального статуса)")
            else:
                lines.append("  ℹ️ Нет активного запуска")
        except Exception as e:
            lines.append(f"  ⚠️ Статус недоступен: {e}")

        return "\n".join(lines)

    def solve(self, env_id: str, steps: int = 0,
              action_name: Optional[str] = None) -> str:
        """
        Запускает игру через arc_play.start_game_async().
        steps=0 → arc_play выберет количество шагов из QML-рекомендации.
        """
        err = self._require_arc_play()
        if err:
            return err

        try:
            result = arc_play.start_game_async(
                env_id=env_id,
                steps=steps,
                render=False,
                action_name=action_name or None,
            )
        except Exception as e:
            return f"❌ Ошибка запуска: {e}"

        if result.get("ok"):
            steps_hint = f"{steps} шагов" if steps else "авто-шаги (QML)"
            return (
                f"🎮 ARC-AGI-3: запущена среда `{env_id}` ({steps_hint})\n"
                f"   → 'arc статус' — текущий прогресс\n"
                f"   → 'arc история' — результаты после завершения"
            )
        return f"⚠️ Не удалось запустить: {result.get('message', result)}"

    def solve_auto(self) -> str:
        """Автовыбор среды и действия (epsilon-greedy policy из arc_play)."""
        err = self._require_arc_play()
        if err:
            return err

        try:
            result = arc_play.start_game_async(env_id="auto", steps=0)
        except Exception as e:
            return f"❌ Ошибка: {e}"

        if result.get("ok"):
            return (
                "🤖 ARC-AGI-3: автозапуск (epsilon-greedy)\n"
                "   → 'arc статус' для результата"
            )
        return f"⚠️ {result.get('message', result)}"

    def history(self) -> str:
        """Статистика прошлых запусков + QML-рекомендации."""
        err = self._require_arc_play()
        if err:
            return err

        try:
            st = arc_play.get_learning_stats()
        except Exception as e:
            return f"❌ Ошибка получения истории: {e}"

        lines = ["📊 ARC-AGI-3 история:"]
        lines.append(
            f"  Запусков: {st.get('runs_total', 0)} | "
            f"Успешных: {st.get('runs_ok', 0)}"
        )
        best = float(st.get("best_score", 0) or 0)
        lines.append(f"  Лучший score: {best:.4f}")
        if st.get("best_env"):
            lines.append(f"  Лучшая среда: {st['best_env']}")
        lines.append(
            f"  Рекомендованных шагов: {st.get('recommended_steps', 10)} "
            f"(режим: {st.get('qml_mode', 'classical')})"
        )
        ibm = st.get("ibm_quantum", "не настроен")
        lines.append(f"  IBM Quantum: {ibm}")

        last = st.get("last")
        if last:
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(last.get("ts", 0)))
            ok_icon = "✅" if last.get("ok") else "❌"
            lines.append(
                f"  Последний: {last.get('env_id', '?')} | {ok_icon} | "
                f"score={float(last.get('score', 0) or 0):.4f} | {ts}"
            )
        return "\n".join(lines)

    def list_envs(self) -> str:
        """Среды из policy + дефолтные."""
        err = self._require_arc_play()
        if err:
            return err

        lines = ["🎮 ARC-AGI-3 — окружения:"]
        try:
            policy = arc_play._load_policy()
            known = policy.get("envs", {})
            if known:
                lines.append("  Из истории:")
                for eid, info in sorted(known.items()):
                    runs = info.get("runs", 0)
                    best = float(info.get("best_score", 0) or 0)
                    ok_r = info.get("ok_runs", 0)
                    lines.append(
                        f"    • {eid}: {runs} запусков, "
                        f"{ok_r} успешных, best={best:.4f}"
                    )
            else:
                lines.append("  (история пуста)")
        except Exception as e:
            lines.append(f"  ⚠️ Политика недоступна: {e}")

        lines.append(f"  Дефолтные среды: {', '.join(ARC3_DEFAULT_ENVS)}")
        lines.append(f"  API: {ARC3_API_BASE}")
        lines.append("  Команда: 'arc решай <env_id>'")
        return "\n".join(lines)

    def stop(self) -> str:
        """
        arc_play запускает игру как daemon-subprocess в .venv_arc.
        Мягкая остановка недоступна из текущего процесса.
        """
        err = self._require_arc_play()
        if err:
            return err

        try:
            st = arc_play.get_status()
        except Exception:
            return "ℹ️ Статус недоступен."

        if st.get("state") != "running":
            return "ℹ️ Нет активного запуска."

        env_id = st.get("env_id", "?")
        return (
            f"⚠️ Среда `{env_id}` запущена в subprocess (.venv_arc).\n"
            "   Прямая остановка недоступна — дождись завершения\n"
            "   или перезапусти ARGOS."
        )

    def llm_analyze(self, env_id: str) -> str:
        """LLM анализирует политику по конкретной среде и даёт рекомендацию."""
        err = self._require_arc_play()
        if err:
            return err
        if not self.core:
            return "❌ Core недоступен — LLM не подключён"

        try:
            policy = arc_play._load_policy()
            env_data = policy.get("envs", {}).get(env_id, {})
            if not env_data:
                return f"ℹ️ Нет истории по среде '{env_id}' — запусти сначала."

            prompt = (
                f"ARC-AGI-3 среда '{env_id}':\n"
                f"  Запусков: {env_data.get('runs', 0)}, "
                f"успешных: {env_data.get('ok_runs', 0)}\n"
                f"  Лучший score: {float(env_data.get('best_score', 0) or 0):.4f}\n"
                f"  Статистика действий: {env_data.get('actions', {})}\n\n"
                "Порекомендуй оптимальную стратегию для следующего запуска. "
                "Кратко (2-3 предложения)."
            )
            result = self.core._ask_ollama("", prompt)
            return f"🤖 Анализ '{env_id}':\n{result or 'Нет ответа'}"
        except Exception as e:
            return f"❌ Ошибка LLM-анализа: {e}"

    def execute(self, text: str = "") -> str:
        """Точка входа SkillLoader."""
        t = (text or "").strip()
        if not t or t.lower() in ("статус", "status", "arc", "arc статус"):
            return self.status()
        # Пробуем маршрутизировать через module-level handle()
        result = handle(t, core=self.core)
        if result is not None:
            return result
        # Без триггера — показываем статус
        return self.status()


# ── Синглтон и handle() ───────────────────────────────────────────────────────
_agent: Optional[ARC3Agent] = None


def handle(text: str, core=None) -> Optional[str]:
    global _agent
    t = text.lower().strip()

    if not any(k in t for k in ["arc", "arc-agi", "arcagi"]):
        return None

    if _agent is None:
        _agent = ARC3Agent(core=core)
    elif core is not None and _agent.core is None:
        _agent.core = core

    # ── Статус ────────────────────────────────────────────────────────────────
    if any(k in t for k in ["arc статус", "arc status", "arc3 статус"]):
        return _agent.status()

    # ── Список сред ───────────────────────────────────────────────────────────
    if any(k in t for k in ["arc среды", "arc список", "arc envs", "arc environments"]):
        return _agent.list_envs()

    # ── Стоп ──────────────────────────────────────────────────────────────────
    if any(k in t for k in ["arc стоп", "arc stop", "arc3 стоп"]):
        return _agent.stop()

    # ── История ───────────────────────────────────────────────────────────────
    if any(k in t for k in ["arc история", "arc stats", "arc статистика"]):
        return _agent.history()

    # ── Автозапуск ────────────────────────────────────────────────────────────
    if any(k in t for k in ["arc авто", "arc auto"]):
        return _agent.solve_auto()

    # ── arc решай <env_id> [N шагов] ─────────────────────────────────────────
    m_solve = re.search(
        r'arc\s+(?:решай|решить|solve|run|запусти)\s+(\S+)(?:\s+(\d+))?', t
    )
    if m_solve:
        env_id = m_solve.group(1)
        steps  = int(m_solve.group(2)) if m_solve.group(2) else 0
        return _agent.solve(env_id, steps)

    # ── arc анализ <env_id> ───────────────────────────────────────────────────
    m_analyze = re.search(r'arc\s+анализ\s+(\S+)', t)
    if m_analyze:
        return _agent.llm_analyze(m_analyze.group(1))

    # ── arc <env_id> напрямую ─────────────────────────────────────────────────
    m_direct = re.search(r'^arc\s+([a-z]{2}\d{2,})\s*(\d+)?$', t)
    if m_direct:
        env_id = m_direct.group(1)
        steps  = int(m_direct.group(2)) if m_direct.group(2) else 0
        return _agent.solve(env_id, steps)

    return None


TRIGGERS = [
    "arc", "arc-agi", "arcagi",
    "arc статус", "arc status",
    "arc среды", "arc envs",
    "arc стоп", "arc stop",
    "arc история", "arc stats",
    "arc авто", "arc auto",
    "arc решай", "arc решить", "arc solve", "arc run",
    "arc анализ",
]


def setup(core=None):
    pass
