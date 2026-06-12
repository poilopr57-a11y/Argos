#!/usr/bin/env python3
"""
smart_model_selector.py — выбор оптимальной модели Claude по сложности задачи.

Адаптировано из шаблона (Telegram 2026-06-06) под РЕАЛЬНЫЙ Anthropic SDK с
ИСПРАВЛЕНИЕМ багов оригинала:
  - `def init(self)` -> `__init__`; `if name == "main"` -> `if __name__ == "__main__"`.
  - Выдуманные ID моделей -> РЕАЛЬНЫЕ из конфига ARGOS (haiku-4-5/sonnet-4-6/opus-4-8).
  - Блокирующий input() убран из горячего пути -> авто-режим + опциональный confirm.
  - Добавлен prompt caching (cache_control ephemeral) с корректным учётом cache-токенов.

Цель: бить Opus только по реально сложным задачам, простое — Haiku, среднее — Sonnet.
Экономия бюджета без потери качества на сложных задачах (FPGA/ARGOS/мультиагент).

Запуск:  python scripts/smart_model_selector.py "почини опечатку в README"
         python scripts/smart_model_selector.py --route-only "оркеструй FPGA инференс"
"""
from __future__ import annotations

import os
import sys
import time

try:  # Windows cp1251 console fix
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
from dataclasses import dataclass, field
from typing import Optional

try:
    from anthropic import Anthropic
except ImportError:
    Anthropic = None


# Реальные модели ARGOS + цены $/1M (input, output) — ориентир 2026.
MODELS = {
    "haiku":  {"id": "claude-haiku-4-5-20251001", "in": 1.0,  "out": 5.0,
               "for": ["fix", "typo", "bug", "review", "test", "format", "rename",
                       "comment", "lint", "check", "simple", "basic", "quick", "опечат"]},
    "sonnet": {"id": "claude-sonnet-4-6",          "in": 3.0,  "out": 15.0,
               "for": ["implement", "refactor", "design", "api", "database", "feature",
                       "optimization", "integration", "migrate", "schema", "внедри"]},
    "opus":   {"id": "claude-opus-4-8",            "in": 15.0, "out": 75.0,
               "for": ["fpga", "multi-agent", "orchestrate", "complex", "argos",
                       "mempalace", "compression", "mcp", "acp", "reasoning",
                       "strategy", "neural", "accelerator", "оркестр", "сложн"]},
}
CACHE_DISCOUNT = 0.9  # кэш-чтение ~ -90% к input-цене


@dataclass
class Selection:
    tier: str
    complexity: str
    model_id: str
    scores: dict = field(default_factory=dict)


def detect_complexity(query: str) -> Selection:
    """Эвристика по ключевым словам. complex >= medium -> opus; medium >= simple -> sonnet."""
    q = query.lower()
    s = {t: sum(1 for kw in m["for"] if kw in q) for t, m in MODELS.items()}
    if s["opus"] > 0 and s["opus"] >= s["sonnet"]:
        tier, comp = "opus", "complex"
    elif s["sonnet"] > 0 and s["sonnet"] >= s["haiku"]:
        tier, comp = "sonnet", "medium"
    else:
        tier, comp = "haiku", "simple"
    return Selection(tier, comp, MODELS[tier]["id"], s)


def estimate_cost(tier: str, in_tok: int, out_tok: int, cached_in: int = 0) -> dict:
    m = MODELS[tier]
    fresh_in = max(0, in_tok - cached_in)
    cost = (fresh_in * m["in"] + cached_in * m["in"] * (1 - CACHE_DISCOUNT)
            + out_tok * m["out"]) / 1_000_000
    opus = ((in_tok * MODELS["opus"]["in"]) + (out_tok * MODELS["opus"]["out"])) / 1_000_000
    return {"cost": round(cost, 5), "opus_cost": round(opus, 5),
            "savings": round(opus - cost, 5),
            "savings_pct": round((opus - cost) / opus * 100, 1) if opus else 0.0}


def execute(query: str, system_prompt: Optional[str] = None,
            confirm: bool = False, max_tokens: int = 2048) -> Optional[str]:
    sel = detect_complexity(query)
    m = MODELS[sel.tier]
    est_in = int(len(query.split()) * 1.4) + 1500
    pre = estimate_cost(sel.tier, est_in, max_tokens)
    print(f"[selector] {sel.complexity.upper()} -> {sel.tier} ({m['id']}) | "
          f"~${pre['cost']:.4f} (vs Opus ${pre['opus_cost']:.4f}, "
          f"экономия {pre['savings_pct']:.0f}%)")
    if confirm and input("execute? (y/n): ").strip().lower() != "y":
        print("отменено"); return None
    if Anthropic is None:
        print("anthropic SDK не установлен — только маршрутизация"); return None
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY не задан"); return None

    client = Anthropic()
    t0 = time.time()
    resp = client.messages.create(
        model=m["id"], max_tokens=max_tokens,
        system=[{"type": "text",
                 "text": system_prompt or "You are ARGOS AI Controller.",
                 "cache_control": {"type": "ephemeral"}}],  # prompt caching
        messages=[{"role": "user", "content": query}],
    )
    u = resp.usage
    cached = getattr(u, "cache_read_input_tokens", 0) or 0
    real = estimate_cost(sel.tier, u.input_tokens, u.output_tokens, cached)
    print(f"[done] in={u.input_tokens} out={u.output_tokens} cache={cached} "
          f"| ${real['cost']:.4f} (-{real['savings_pct']:.0f}% vs Opus) "
          f"| {time.time()-t0:.1f}s")
    return resp.content[0].text if resp.content else None


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    route_only = "--route-only" in sys.argv
    if not args:
        # самотест без API
        for q in ["почини опечатку в readme", "внедри REST API для логов",
                  "оркеструй FPGA инференс через mempalace"]:
            sel = detect_complexity(q)
            print(f"  '{q[:35]}' -> {sel.tier} ({sel.complexity})  scores={sel.scores}")
        sys.exit(0)
    query = " ".join(args)
    if route_only:
        sel = detect_complexity(query)
        print(f"{sel.tier} -> {sel.model_id} ({sel.complexity})")
    else:
        out = execute(query, confirm=False)
        if out:
            print("\n" + out)
