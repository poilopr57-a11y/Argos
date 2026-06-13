"""
desktop_actions.py — Управление ПК через Telegram (мышь, клавиатура, скриншот)
═══════════════════════════════════════════════════════════════════════════════
Команды:
  мышь move 100 200          — переместить курсор
  мышь click [X Y]           — левый клик (на текущей позиции или X Y)
  мышь rclick [X Y]          — правый клик
  мышь dclick [X Y]          — двойной клик
  мышь scroll 3              — прокрутка вверх (отрицательное — вниз)
  мышь drag 100 200 300 400  — перетащить с (100,200) в (300,400)
  мышь позиция               — текущие координаты курсора
  клавиша enter              — нажать клавишу
  клавиша ctrl+c             — комбинация клавиш
  печатай Привет мир         — напечатать текст
  горячие клавиши            — список поддерживаемых клавиш
  скриншот                   — скриншот экрана
  экран статус               — статус модуля управления
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

SKILL_DESCRIPTION = "Управление ПК: мышь, клавиатура, скриншот"

import os
import sys
import re
import time
from pathlib import Path
from typing import Optional

from src.argos_logger import get_logger

log = get_logger("argos.desktop_actions")

# Добавляем корень проекта в sys.path для импорта src.input_control
_PROJ = Path(__file__).parent.parent.parent
if str(_PROJ) not in sys.path:
    sys.path.insert(0, str(_PROJ))


def _get_controller():
    """Ленивая загрузка InputController."""
    try:
        from src.input_control import InputController
        return InputController()
    except Exception as e:
        log.warning("InputController недоступен: %s", e)
        return None


_HELP_KEYS = (
    "enter, esc, tab, space, backspace, delete, up, down, left, right\n"
    "f1–f12, home, end, pageup, pagedown, insert, printscreen\n"
    "ctrl+c, ctrl+v, ctrl+z, ctrl+s, ctrl+a, ctrl+f, ctrl+w\n"
    "alt+f4, win, win+d, win+l, ctrl+alt+del"
)


class DesktopActionsSkill:
    """Навык управления мышью и клавиатурой через Telegram."""

    def __init__(self, core=None):
        self.core = core
        self._ic = None  # ленивая инициализация

    def _controller(self):
        if self._ic is None:
            self._ic = _get_controller()
        return self._ic

    def handle(self, text: str) -> Optional[str]:
        """Разбирает команду и выполняет действие."""
        t = text.strip().lower()

        # ── мышь ────────────────────────────────────────────────────────────
        if t.startswith("мышь") or t.startswith("mouse"):
            return self._mouse(text)

        # ── клавиша ─────────────────────────────────────────────────────────
        if t.startswith("клавиша") or t.startswith("key ") or t.startswith("нажми"):
            parts = text.split(None, 1)
            key = parts[1].strip() if len(parts) > 1 else ""
            if not key:
                return "❌ Укажите клавишу: клавиша enter"
            ic = self._controller()
            if ic is None:
                return self._no_pyautogui()
            return ic.press(key)

        # ── печатай ─────────────────────────────────────────────────────────
        if t.startswith("печатай") or t.startswith("type "):
            parts = text.split(None, 1)
            msg = parts[1] if len(parts) > 1 else ""
            if not msg:
                return "❌ Укажите текст: печатай Привет мир"
            ic = self._controller()
            if ic is None:
                return self._no_pyautogui()
            return ic.type_text(msg)

        # ── скриншот ─────────────────────────────────────────────────────────
        if "скриншот" in t or "screenshot" in t:
            ic = self._controller()
            if ic is None:
                return self._no_pyautogui()
            save_path = f"data/screenshots/screen_{int(time.time())}.png"
            Path("data/screenshots").mkdir(parents=True, exist_ok=True)
            return ic.screenshot(save_path)

        # ── горячие клавиши / справка ────────────────────────────────────────
        if "горячие клавиши" in t or "hot keys" in t or "справка мышь" in t:
            return f"⌨️ Поддерживаемые клавиши:\n{_HELP_KEYS}"

        # ── статус ───────────────────────────────────────────────────────────
        if "экран статус" in t or "desktop status" in t or "статус мышь" in t:
            ic = self._controller()
            if ic is None:
                return self._no_pyautogui()
            avail = ic.is_available()
            pos = ic.position() if avail else "н/д"
            return (
                f"🖥️ Desktop Actions:\n"
                f"  pyautogui: {'✅' if avail else '❌'}\n"
                f"  позиция: {pos}\n"
                f"  safe_mode: {os.getenv('ARGOS_INPUT_SAFE', 'on')}"
            )

        return None  # не наша команда

    def _mouse(self, text: str) -> str:
        ic = self._controller()
        if ic is None:
            return self._no_pyautogui()

        t = text.strip().lower()
        nums = [int(x) for x in re.findall(r"-?\d+", text)]

        if "move" in t or "перемест" in t:
            if len(nums) >= 2:
                return ic.move(nums[0], nums[1])
            return "❌ Укажи координаты: мышь move 100 200"

        elif "rclick" in t or "правый клик" in t or "right click" in t:
            x, y = (nums[0], nums[1]) if len(nums) >= 2 else (None, None)
            return ic.right_click(x, y)

        elif "dclick" in t or "двойной клик" in t or "double click" in t:
            x, y = (nums[0], nums[1]) if len(nums) >= 2 else (None, None)
            return ic.double_click(x, y)

        elif "click" in t or "клик" in t:
            x, y = (nums[0], nums[1]) if len(nums) >= 2 else (None, None)
            return ic.click(x, y)

        elif "scroll" in t or "прокрут" in t:
            clicks = nums[0] if nums else 3
            return ic.scroll(clicks)

        elif "drag" in t or "перетащ" in t:
            if len(nums) >= 4:
                return ic.drag(nums[0], nums[1], nums[2], nums[3])
            return "❌ Укажи координаты: мышь drag 100 200 300 400"

        elif "позиция" in t or "position" in t or "где" in t:
            return ic.position()

        return (
            "🖱️ Команды мыши:\n"
            "  мышь move X Y\n"
            "  мышь click [X Y]\n"
            "  мышь rclick [X Y]\n"
            "  мышь dclick [X Y]\n"
            "  мышь scroll N\n"
            "  мышь drag X1 Y1 X2 Y2\n"
            "  мышь позиция"
        )

    def execute(self, text: str = "") -> str:
        """Точка входа SkillLoader."""
        t = (text or "").strip()
        if not t or t.lower() in ("статус", "status", "помощь", "help"):
            ic = self._controller()
            if ic is None:
                return self._no_pyautogui()
            pos = ic.position()
            return f"🖥️ DesktopActions: активен\n  Позиция мыши: {pos}\n  Команды: мышь, клавиша, печатай, скриншот"
        return self.handle(t) or f"❓ Неизвестная команда desktop: {t[:50]}"

    @staticmethod
    def _no_pyautogui() -> str:
        return (
            "❌ pyautogui не установлен.\n"
            "Установи: pip install pyautogui pyperclip\n"
            "Затем перезапусти ARGOS."
        )


TRIGGERS = [
    "мышь", "mouse", "клавиша", "key ", "нажми", "печатай", "type ",
    "скриншот", "screenshot", "горячие клавиши", "hot keys",
    "экран статус", "desktop status",
]

_skill_instance = None


def handle(text: str, core=None) -> str | None:
    global _skill_instance
    t = text.strip().lower()
    if not any(tr in t for tr in TRIGGERS):
        return None
    if _skill_instance is None:
        _skill_instance = DesktopActionsSkill(core=core)
    return _skill_instance.handle(text) or None


def setup(core=None):
    pass
