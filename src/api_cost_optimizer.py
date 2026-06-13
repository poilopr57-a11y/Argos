"""
src/api_cost_optimizer.py — Оптимизация затрат на API
Реализует:
1. Semantic Caching — кэширование по смыслу запроса
2. Model Tiering — автоматический выбор модели по сложности
"""

import hashlib
import time
import logging
import os
import threading
from typing import Optional

# SentenceTransformer: грузим ТОЛЬКО если явно включён флаг ARGOS_SEMANTIC_CACHE=1
# По умолчанию выключен — загрузка 80MB модели вешает CPU на 2 мин при старте
_SEMANTIC_CACHE_ENABLED = os.getenv("ARGOS_SEMANTIC_CACHE", "0").strip() in ("1", "true", "on", "yes")

HAS_EMBEDDINGS = False
SentenceTransformer = None  # placeholder

if _SEMANTIC_CACHE_ENABLED:
    try:
        from sentence_transformers import SentenceTransformer as _ST
        SentenceTransformer = _ST
        HAS_EMBEDDINGS = True
    except ImportError:
        pass

log = logging.getLogger("argos.api_cost")

SIMPLE_KEYWORDS = [
    "который", "какой", "что", "где", "когда", "почему", "зачем",
    "статус", "время", "дата", "сегодня", "сейчас",
    "список", "покажи", "дай", "есть", "найди",
]

COMPLEX_KEYWORDS = [
    "анализ", "сравни", "объясни", "разработай", "создай",
    "напиши код", "программа", "алгоритм", "оптимизируй",
    "рассуждение", "вывод", "логика", "математика",
]

TIER_FAST = "fast"      # GPT-4o-mini, Gemini Flash
TIER_MEDIUM = "medium"  # GPT-4o, Gemini Pro
TIER_HEAVY = "heavy"    # Claude 3.5 Sonnet, GPT-4 Turbo


class SemanticCache:
    """Кэш семантических запросов с embeddings."""
    
    def __init__(self, db_path: str = "data/semantic_cache.db", threshold: float = 0.985):
        # 0.985 — строгий порог: только почти идентичные по смыслу запросы.
        # Низкий порог (0.92) давал "одинаковые ответы на разные команды".
        self.threshold = threshold
        self._embedder = None
        self._cache: dict[str, tuple[str, float]] = {}  # hash -> (response, timestamp)
        self._vectors: dict[str, list[float]] = {}
        self._db_path = db_path
        self._load_cache()
        
        if HAS_EMBEDDINGS and SentenceTransformer is not None:
            # Грузим модель в daemon-потоке с таймаутом 30с — не блокируем запуск
            model_name = os.getenv("EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
            done_event = threading.Event()

            def _load_embedder():
                try:
                    self._embedder = SentenceTransformer(model_name)
                    log.info("[SemanticCache] Embeddings ready: %s", model_name)
                except Exception as exc:
                    log.warning("[SemanticCache] Embeddings failed: %s", exc)
                finally:
                    done_event.set()

            t = threading.Thread(target=_load_embedder, daemon=True, name="semcache-init")
            t.start()
            if not done_event.wait(timeout=30):
                log.warning("[SemanticCache] Embeddings timeout 30s — работаем без векторов")
    
    def _load_cache(self):
        import json
        try:
            os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
            with open(self._db_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                self._cache = {k: (v[0], v[1]) for k, v in data.items()}
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning("[SemanticCache] Load failed: %s", e)
    
    def _save_cache(self):
        import json
        try:
            with open(self._db_path, "w", encoding="utf-8") as f:
                json.dump({k: [v[0], v[1]] for k, v in self._cache.items()}, f)
        except Exception as e:
            log.warning("[SemanticCache] Save failed: %s", e)
    
    def _get_hash(self, text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()[:16]
    
    def get(self, query: str) -> Optional[str]:
        """Получить кэшированный ответ."""
        q_lower = query.lower()
        for q_cache in self._cache:
            if q_lower == q_cache:
                return self._cache[q_cache][0]

        # Короткие запросы (команды, "кто ты", "статус") НЕ матчим семантически —
        # у них эмбеддинги похожи и дают ложные hit'ы (одинаковые ответы на разные команды).
        if len(q_lower.strip()) < 25:
            return None

        if self._embedder and self._vectors:
            try:
                emb = self._embedder.encode(query)
                best_score = 0
                best_response = None
                for cached_q, vec in self._vectors.items():
                    score = self._cosine_sim(emb, vec)
                    if score > best_score and score >= self.threshold:
                        best_score = score
                        best_response = self._cache[cached_q][0]
                if best_response:
                    log.info("[SemanticCache] HIT: %.2f similarity", best_score)
                    return best_response
            except Exception as e:
                log.debug("[SemanticCache] Embed error: %s", e)
        return None
    
    def _cosine_sim(self, a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5
        return dot / (norm_a * norm_b) if norm_a and norm_b else 0
    
    def set(self, query: str, response: str):
        """Сохранить ответ в кэш."""
        self._cache[query.lower()] = (response, time.time())
        if self._embedder and len(self._cache) < 10000:
            try:
                self._vectors[query.lower()] = self._embedder.encode(query).tolist()
            except Exception:
                pass
        if len(self._cache) % 100 == 0:
            self._save_cache()
    
    def clear(self):
        self._cache.clear()
        self._vectors.clear()
        self._save_cache()


class ModelTierRouter:
    """Роутер выбора модели по сложности запроса."""
    
    def __init__(self):
        self._cache = SemanticCache()
        self._tier_map = {
            TIER_FAST: ["gpt-4o-mini", "gemini-2.5-flash", "llama3.1:8b"],
            TIER_MEDIUM: ["gpt-4o", "gemini-2.5-flash-lite", "claude-3-haiku"],
            TIER_HEAVY: ["gpt-4-turbo", "claude-3.5-sonnet", "gemini-2.5-flash"],
        }
    
    def estimate_tier(self, query: str) -> str:
        """Оценить сложность и вернуть tier."""
        q_lower = query.lower()
        
        complex_count = sum(1 for kw in COMPLEX_KEYWORDS if kw in q_lower)
        simple_count = sum(1 for kw in SIMPLE_KEYWORDS if kw in q_lower)
        
        word_count = len(q_lower.split())
        
        if complex_count >= 2 or word_count > 100:
            return TIER_HEAVY
        if simple_count >= 3 or complex_count >= 1 or word_count > 30:
            return TIER_MEDIUM
        return TIER_FAST
    
    def get_model(self, query: str) -> tuple[str, str]:
        """Вернуть (tier, model_name)."""
        tier = self.estimate_tier(query)
        models = self._tier_map.get(tier, self._tier_map[TIER_FAST])
        return tier, models[0]
    
    def cached_response(self, query: str) -> Optional[str]:
        return self._cache.get(query)
    
    def store_response(self, query: str, response: str):
        self._cache.set(query, response)


_router = None


def get_router() -> ModelTierRouter:
    global _router
    if _router is None:
        _router = ModelTierRouter()
    return _router


def get_tier_and_model(query: str) -> tuple[str, str]:
    return get_router().get_model(query)


def get_cached(query: str) -> Optional[str]:
    return get_router().cached_response(query)


def store_cached(query: str, response: str):
    get_router().store_response(query, response)
