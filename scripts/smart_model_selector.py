#!/usr/bin/env python3
"""
Token-Optimized Model Selector для ARGOS AI
Автоматически выбирает оптимальную модель для экономии 70-98% бюджета

Расценки (2026-06):
  claude-3-5-haiku-20241022       $0.80 / 1M input,  $4.00 / 1M output
  claude-3-5-sonnet-20241022      $3.00 / 1M input,  $15.00 / 1M output
  claude-opus-4-5                 $15.00 / 1M input, $75.00 / 1M output

Prompt caching: -90% на повторных системных промптах.
"""

import os
import sys
from anthropic import Anthropic
from typing import Optional
from datetime import datetime

# headroom compress — сжимаем запрос ДО отправки модели
def _headroom_compress(text: str) -> tuple[str, int]:
    """Сжать текст через headroom или caveman. Возвращает (сжатый, сэкономлено_токенов)."""
    original_len = len(text)
    # 1. Попробовать headroom-ai (если установлен)
    try:
        import headroom as _hr
        msgs = [{"role": "user", "content": text}]
        result = _hr.compress(msgs)
        compressed = result.messages[0]["content"] if result.messages else text
        saved = original_len - len(compressed)
        if saved > 0:
            return compressed, saved
    except Exception:
        pass
    # 2. Fallback: caveman_compress
    try:
        _scripts_headroom = os.path.join(os.path.dirname(__file__), "headroom")
        if _scripts_headroom not in sys.path:
            sys.path.insert(0, _scripts_headroom)
        from caveman_compress import CavemanCompressor
        cc = CavemanCompressor(level=2)
        r = cc.compress(text)
        saved = original_len - len(r.compressed)
        if saved > 50:
            return r.compressed, saved
    except Exception:
        pass
    return text, 0


# ─────────────────────────────────────────────────────────────────────────────
# ARGOS system prompt (кэшируется — повторные запросы -90%)
# ─────────────────────────────────────────────────────────────────────────────
ARGOS_SYSTEM_PROMPT = """You are ARGOS AI Controller — an advanced self-replicating AI ecosystem integrating:
- MemPalace with 41,926 drawers (sqlite+numpy backend)
- FPGA acceleration (XC7A200T M.2 Artix-7 + DDR3 512MB)
- Headroom Compression (95% token savings)
- MCP/ACP protocols (100+ tools)
- VPN/DNS privacy stack (Cloudflare / WireGuard)
- Home Assistant integration
- 33+ sub-agents (Qwen2.5 LoRA fine-tuned argos-v1)
- V100 16GB for training; RX580/Vega11/RX560 for inference

Provide technical, data-driven responses with metrics. Be concise."""


class SmartModelSelector:
    """Выбирает правильную модель (Haiku/Sonnet/Opus) для каждой задачи."""

    def __init__(self):
        self.client = Anthropic()

        self.models = {
            "haiku": {
                "id": "claude-3-5-haiku-20241022",
                "cost_per_1m_in":  0.80,
                "cost_per_1m_out": 4.00,
                "best_for": ["code_review", "debugging", "testing", "formatting",
                             "simple", "rename", "lint", "comment"],
            },
            "sonnet": {
                "id": "claude-3-5-sonnet-20241022",
                "cost_per_1m_in":  3.00,
                "cost_per_1m_out": 15.00,
                "best_for": ["implementation", "design", "refactoring", "api",
                             "database", "integration", "feature", "medium"],
            },
            "opus": {
                "id": "claude-opus-4-5",
                "cost_per_1m_in":  15.00,
                "cost_per_1m_out": 75.00,
                "best_for": ["complex", "fpga", "multi-agent", "orchestrate",
                             "argos", "mempalace", "mcp", "acp", "deep",
                             "reasoning", "strategy", "neural", "accelerator",
                             "challenging", "novel", "breakthrough"],
            },
        }

        self.usage_log: list[dict] = []
        self.session_cost: float = 0.0

    # ──────────────────────────────────────────────────────────────────────────
    def detect_complexity(self, query: str) -> tuple[str, str]:
        """Вернуть (model_key, complexity_label)."""
        q = query.lower()

        simple_kw  = {"fix", "typo", "bug", "review", "test", "format",
                      "rename", "comment", "lint", "check", "simple",
                      "basic", "easy", "quick", "list", "show", "print"}
        medium_kw  = {"implement", "refactor", "design", "api", "database",
                      "optimiz", "integrat", "architect", "migrat",
                      "feature", "schema", "query", "module", "class"}
        complex_kw = {"fpga", "multi-agent", "orchestrat", "complex",
                      "argos", "mempalace", "compress", "mcp", "acp",
                      "deep", "reasoning", "strategy", "neural", "accelerat",
                      "challeng", "novel", "breakthrough", "lora", "finetun",
                      "gguf", "vivado", "bitstream", "xdma"}

        def score(kws):
            return sum(1 for kw in kws if kw in q)

        s, m, c = score(simple_kw), score(medium_kw), score(complex_kw)

        if c > 0 and c >= m:
            return "opus", "complex"
        elif m > 0 and m >= s:
            return "sonnet", "medium"
        else:
            return "haiku", "simple"

    # ──────────────────────────────────────────────────────────────────────────
    def estimate_cost(self, model: str, estimated_tokens: int) -> dict:
        mi        = self.models[model]
        cost      = (estimated_tokens / 1_000_000) * mi["cost_per_1m_in"]
        opus_cost = (estimated_tokens / 1_000_000) * self.models["opus"]["cost_per_1m_in"]
        savings   = opus_cost - cost
        pct       = (savings / opus_cost * 100) if opus_cost > 0 else 0
        return {
            "model": mi["id"],
            "estimated_tokens": estimated_tokens,
            "estimated_cost": cost,
            "opus_cost_comparison": opus_cost,
            "savings": savings,
            "savings_percent": pct,
        }

    # ──────────────────────────────────────────────────────────────────────────
    def execute_query(
        self,
        user_query: str,
        system_prompt: Optional[str] = None,
        force_model: Optional[str] = None,
        no_confirm: bool = False,
    ) -> Optional[str]:
        """Выполнить запрос с оптимальной (или заданной) моделью."""

        model_choice = force_model or self.detect_complexity(user_query)[0]
        complexity   = "forced" if force_model else self.detect_complexity(user_query)[1]
        mi           = self.models[model_choice]

        est_tokens = int(len(user_query.split()) * 1.5 + 2000)
        estimate   = self.estimate_cost(model_choice, est_tokens)

        print(f"""
╔══════════════════════════════════════════════════════════╗
║           🤖 ARGOS AI — Smart Model Selector            ║
╠══════════════════════════════════════════════════════════╣
║
║  📊 Task Analysis:
║  ├─ Complexity : {complexity.upper()}
║  ├─ Model      : {model_choice.upper()}
║  └─ Model ID   : {mi['id']}
║
║  💰 Cost Estimation (input only):
║  ├─ Est. tokens : {estimate['estimated_tokens']:,}
║  ├─ This model  : ${estimate['estimated_cost']:.4f}
║  ├─ With Opus   : ${estimate['opus_cost_comparison']:.4f}
║  └─ 💾 SAVINGS  : ${estimate['savings']:.4f} ({estimate['savings_percent']:.1f}%)
║
║  ✅ Best for    : {', '.join(mi['best_for'][:4])}
║
╚══════════════════════════════════════════════════════════╝""")

        if not no_confirm:
            confirm = input("\n▶️  Execute? (y/n): ").strip().lower()
            if confirm != "y":
                print("❌ Cancelled")
                return None

        print(f"\n📤 Sending to {model_choice.upper()}...\n")

        sys_text = system_prompt or ARGOS_SYSTEM_PROMPT

        # 🗜️ Headroom compression — сжать запрос ДО отправки
        compressed_query, saved_chars = _headroom_compress(user_query)
        if saved_chars > 0:
            saved_tokens_est = saved_chars // 4
            saved_cost_est   = saved_tokens_est * mi["cost_per_1m_in"] / 1_000_000
            print(f"🗜️  Headroom: -{saved_chars} chars (≈-{saved_tokens_est} tokens, -${saved_cost_est:.4f})")
            user_query = compressed_query

        response = self.client.messages.create(
            model=mi["id"],
            max_tokens=4096,
            system=[
                {
                    "type": "text",
                    "text": sys_text,
                    "cache_control": {"type": "ephemeral"},  # 🎯 prompt caching
                }
            ],
            messages=[{"role": "user", "content": user_query}],
        )

        usage       = response.usage
        cache_read  = getattr(usage, "cache_read_input_tokens",      0)
        cache_write = getattr(usage, "cache_creation_input_tokens",  0)

        in_cost      = usage.input_tokens  * mi["cost_per_1m_in"]  / 1_000_000
        out_cost     = usage.output_tokens * mi["cost_per_1m_out"] / 1_000_000
        cache_saving = cache_read * mi["cost_per_1m_in"] / 1_000_000 * 0.90
        total_cost   = in_cost + out_cost - cache_saving

        self.session_cost += total_cost
        self.usage_log.append({
            "ts":            datetime.utcnow().isoformat(),
            "model":         model_choice,
            "input_tokens":  usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_read":    cache_read,
            "cache_write":   cache_write,
            "cost":          total_cost,
        })

        print(f"""
╔══════════════════════════════════════════════════════════╗
║              ✅ Query Completed                         ║
╠══════════════════════════════════════════════════════════╣
║
║  📊 Actual Usage:
║  ├─ Input        : {usage.input_tokens:,}
║  ├─ Output       : {usage.output_tokens:,}
║  ├─ Cache read   : {cache_read:,}  (-90% на этих токенах)
║  └─ Cache write  : {cache_write:,}
║
║  💰 Actual Cost:
║  ├─ Input+Output : ${in_cost + out_cost:.4f}
║  ├─ Cache saving : -${cache_saving:.4f}
║  └─ ✅ TOTAL     : ${total_cost:.4f}
║
║  📈 Session total: ${self.session_cost:.4f}
║
╚══════════════════════════════════════════════════════════╝""")

        return response.content[0].text

    # ──────────────────────────────────────────────────────────────────────────
    def batch_queries(self, queries: list[dict], no_confirm: bool = False) -> list:
        print(f"\n📦 Processing {len(queries)} queries...\n")
        results = []
        for i, q in enumerate(queries, 1):
            print(f"\n{'─'*60}\nQuery {i}/{len(queries)}\n{'─'*60}")
            r = self.execute_query(
                q.get("query", ""),
                q.get("system_prompt"),
                q.get("force_model"),
                no_confirm=no_confirm,
            )
            results.append(r)
        return results

    # ──────────────────────────────────────────────────────────────────────────
    def session_report(self):
        if not self.usage_log:
            print("No queries this session.")
            return
        total_in  = sum(e["input_tokens"]  for e in self.usage_log)
        total_out = sum(e["output_tokens"] for e in self.usage_log)
        print(f"""
╔══════════════════════════════════════════════════════════╗
║                 📊 Session Report                       ║
╠══════════════════════════════════════════════════════════╣
║  Queries    : {len(self.usage_log)}
║  Input tok  : {total_in:,}
║  Output tok : {total_out:,}
║  Session $  : ${self.session_cost:.4f}
╚══════════════════════════════════════════════════════════╝""")
        for e in self.usage_log:
            print(f"  [{e['ts']}] {e['model']:6s}  "
                  f"in={e['input_tokens']:5d}  out={e['output_tokens']:4d}  "
                  f"cache={e['cache_read']:5d}  ${e['cost']:.4f}")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────
def main():
    print("""
╔══════════════════════════════════════════════════════════╗
║     🚀 ARGOS AI — Token-Optimized Query System          ║
║                                                          ║
║  Экономит 70-98% бюджета через smart model selection    ║
║  Haiku ($0.80/1M) | Sonnet ($3/1M) | Opus ($15/1M)     ║
╚══════════════════════════════════════════════════════════╝""")

    selector = SmartModelSelector()

    while True:
        print("\n" + "─"*60)
        print("  1. Single query (auto model)")
        print("  2. Single query (force model: haiku/sonnet/opus)")
        print("  3. Batch queries")
        print("  4. Session report")
        print("  5. Exit")
        print("─"*60)

        choice = input("▶️  Choose (1-5): ").strip()

        if choice == "1":
            q = input("💬 Query: ").strip()
            if q:
                r = selector.execute_query(q)
                if r:
                    print(f"\n📝 Response:\n{r}")

        elif choice == "2":
            m = input("Model (haiku/sonnet/opus): ").strip().lower()
            if m not in selector.models:
                print("❌ Unknown model"); continue
            q = input("💬 Query: ").strip()
            if q:
                r = selector.execute_query(q, force_model=m)
                if r:
                    print(f"\n📝 Response:\n{r}")

        elif choice == "3":
            queries = []
            print("📦 Enter queries (empty line to finish):")
            while True:
                q = input("> ").strip()
                if not q:
                    break
                queries.append({"query": q})
            if queries:
                selector.batch_queries(queries, no_confirm=True)

        elif choice == "4":
            selector.session_report()

        elif choice == "5":
            selector.session_report()
            print("\n👋 Goodbye!")
            break

        else:
            print("❌ Invalid choice")


if __name__ == "__main__":
    main()
