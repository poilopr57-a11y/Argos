"""
src/mempalace_bridge.py — ARGOS ↔ MemPalace Integration

Bridge ARGOS to the real MemPalace package living in ``mempalace-develop/``.
Keeps the old public API:
  - get_memory_context()
  - store_memory()
  - search_memory()
  - status()
"""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("argos.mempalace")

_MEMPALACE_ENABLED = os.getenv("ARGOS_MEMPALACE", "1").strip().lower() in {"1", "true", "on", "yes"}
_REPO_ROOT = Path(__file__).resolve().parent.parent
_MEMPALACE_SRC = _REPO_ROOT / "mempalace-develop"
_PALACE_PATH = os.getenv(
    "MEMPALACE_PALACE_PATH",
    str(_REPO_ROOT / "data" / "mempalace"),
)
_FALLBACK_JSONL = Path(
    os.getenv(
        "MEMPALACE_FALLBACK_JSONL",
        str(_REPO_ROOT / "data" / "mempalace" / "entity_fallback.jsonl"),
    )
)
_IDENTITY_PATH = os.getenv(
    "MEMPALACE_IDENTITY",
    str(_REPO_ROOT / ".mempalace" / "identity.txt"),
)
_COLLECTION = "mempalace_drawers"
_VEC_DB_PATH = _REPO_ROOT / "data" / "mempalace" / "mempalace_vec.sqlite3"


def _touch_vec_db(drawer_ids: list[str]) -> None:
    """Обновляет last_access_ts для Dry Leaf — предотвращает выгрузку тёплых векторов."""
    if not _VEC_DB_PATH.exists() or not drawer_ids:
        return
    try:
        import sqlite3, time
        conn = sqlite3.connect(str(_VEC_DB_PATH), timeout=3)
        now = time.time()
        for did in drawer_ids:
            try:
                conn.execute("UPDATE drawers SET last_access_ts=? WHERE id=?", (now, did))
            except Exception:
                pass
        conn.commit()
        conn.close()
    except Exception:
        pass


class _DeterministicEmbedding:
    """Детерминированный эмбеддинг (хеш→вектор 384d) — БЕЗ onnxruntime.
    chromadb по умолчанию тянет ONNX MiniLM, который сегфолтит main при
    многопоточной загрузке skills. Этот embedding не требует нативных либ."""

    _DIM = 384

    def __call__(self, input):
        import hashlib, struct, math
        if isinstance(input, str):
            input = [input]
        out = []
        for text in input:
            text = (text or "").lower()
            # Bag-of-words хеш: каждое слово → вклад в вектор (семантика по словам)
            vec = [0.0] * self._DIM
            words = text.split()
            for w in words:
                h = hashlib.md5(w.encode("utf-8")).digest()
                for i in range(0, len(h), 4):
                    idx = struct.unpack("I", h[i:i+4])[0] % self._DIM
                    sign = 1.0 if (h[i] & 1) else -1.0
                    vec[idx] += sign
            # нормализация
            norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            out.append([x / norm for x in vec])
        return out

    def name(self):
        return "deterministic-bow-384"

class _VecStore:
    """Надёжное векторное хранилище MemPalace на sqlite3 + numpy.

    Зачем своё, а не chromadb: chromadb 0.6.3 на этой машине нестабилен —
    его нативный producer-поток даёт «resource deadlock» и SEGFAULT при
    многопоточной загрузке skills, а DefaultEmbeddingFunction тянет onnxruntime
    (тоже segfault). Здесь: только stdlib sqlite3 + numpy, эмбеддинги
    детерминированные (bag-of-words hash 384d), поиск — косинус по матрице
    в памяти. Никаких нативных потоков → ни segfault, ни deadlock.

    Интерфейс совместим с chromadb Collection (count/get/upsert/add/delete/query),
    чтобы остальной код bridge не переписывать.
    """

    _DIM = 384

    def __init__(self, db_path: str, embed):
        import sqlite3
        import numpy as np
        self._np = np
        self._embed = embed
        self._lock = threading.RLock()
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS drawers("
            "id TEXT PRIMARY KEY, document TEXT, wing TEXT, room TEXT,"
            "source TEXT, source_file TEXT, entity_id TEXT, importance REAL,"
            "ts INTEGER, filed_at TEXT, emb BLOB)"
        )
        self._db.commit()
        self._ids: list[str] = []
        self._docs: list[str] = []
        self._metas: list[dict] = []
        self._index: dict[str, int] = {}
        self._mat = np.zeros((0, self._DIM), dtype=np.float32)
        self._load()

    # ---- внутреннее ----
    def _vec(self, text: str):
        return self._np.asarray(self._embed([text or ""])[0], dtype=self._np.float32)

    def _meta_from_row(self, r) -> dict:
        return {
            "wing": r[2] or "", "room": r[3] or "", "source": r[4] or "",
            "source_file": r[5] or "", "entity_id": r[6] or "",
            "importance": r[7] if r[7] is not None else 3.0,
            "ts": r[8] or 0, "filed_at": r[9] or "",
        }

    def _load(self):
        with self._lock:
            cur = self._db.execute(
                "SELECT id,document,wing,room,source,source_file,entity_id,"
                "importance,ts,filed_at,emb FROM drawers"
            )
            ids, docs, metas, embs = [], [], [], []
            for r in cur.fetchall():
                ids.append(r[0]); docs.append(r[1] or "")
                metas.append(self._meta_from_row(r))
                if r[10]:
                    embs.append(self._np.frombuffer(r[10], dtype=self._np.float32))
                else:
                    embs.append(self._np.zeros(self._DIM, dtype=self._np.float32))
            self._ids, self._docs, self._metas = ids, docs, metas
            self._index = {i: k for k, i in enumerate(ids)}
            if embs:
                self._mat = self._np.vstack(embs).astype(self._np.float32)
            else:
                self._mat = self._np.zeros((0, self._DIM), dtype=self._np.float32)

    @staticmethod
    def _match(meta: dict, where) -> bool:
        if not where:
            return True
        for k, v in where.items():
            if str(meta.get(k, "")) != str(v):
                return False
        return True

    # ---- chromadb-совместимый API ----
    def count(self) -> int:
        return len(self._ids)

    def upsert(self, documents=None, ids=None, metadatas=None):
        documents = documents or []; ids = ids or []
        metadatas = metadatas or [{} for _ in ids]
        with self._lock:
            for doc, _id, meta in zip(documents, ids, metadatas):
                meta = meta or {}
                v = self._vec(doc)
                m = {
                    "wing": meta.get("wing", ""), "room": meta.get("room", ""),
                    "source": meta.get("source", ""), "source_file": meta.get("source_file", ""),
                    "entity_id": meta.get("entity_id", ""),
                    "importance": float(meta.get("importance", 3.0) or 3.0),
                    "ts": int(meta.get("ts", 0) or 0), "filed_at": meta.get("filed_at", ""),
                }
                self._db.execute(
                    "INSERT INTO drawers(id,document,wing,room,source,source_file,"
                    "entity_id,importance,ts,filed_at,emb) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET document=excluded.document,"
                    "wing=excluded.wing,room=excluded.room,source=excluded.source,"
                    "source_file=excluded.source_file,entity_id=excluded.entity_id,"
                    "importance=excluded.importance,ts=excluded.ts,"
                    "filed_at=excluded.filed_at,emb=excluded.emb",
                    (_id, doc, m["wing"], m["room"], m["source"], m["source_file"],
                     m["entity_id"], m["importance"], m["ts"], m["filed_at"], v.tobytes()),
                )
                if _id in self._index:
                    k = self._index[_id]
                    self._docs[k] = doc; self._metas[k] = m; self._mat[k] = v
                else:
                    self._index[_id] = len(self._ids)
                    self._ids.append(_id); self._docs.append(doc); self._metas.append(m)
                    self._mat = (self._np.vstack([self._mat, v[None, :]])
                                 if self._mat.shape[0] else v[None, :].copy())
            self._db.commit()

    add = upsert

    def delete(self, ids=None):
        ids = ids or []
        with self._lock:
            for _id in ids:
                self._db.execute("DELETE FROM drawers WHERE id=?", (_id,))
            self._db.commit()
            self._load()

    def get(self, where=None, include=None, limit=None):
        with self._lock:
            docs, metas, out_ids = [], [], []
            for i, _id in enumerate(self._ids):
                if self._match(self._metas[i], where):
                    docs.append(self._docs[i]); metas.append(dict(self._metas[i]))
                    out_ids.append(_id)
                    if limit and len(out_ids) >= limit:
                        break
            return {"documents": docs, "metadatas": metas, "ids": out_ids}

    def query(self, query_texts=None, n_results=5, where=None):
        with self._lock:
            q = (query_texts or [""])[0]
            if self._mat.shape[0] == 0:
                return {"documents": [[]], "metadatas": [[]], "distances": [[]], "ids": [[]]}
            qv = self._vec(q)
            scores = self._mat @ qv
            order = self._np.argsort(-scores)
            docs, metas, dists, ids = [], [], [], []
            for k in order:
                k = int(k)
                if where and not self._match(self._metas[k], where):
                    continue
                docs.append(self._docs[k]); metas.append(dict(self._metas[k]))
                dists.append(float(1.0 - scores[k])); ids.append(self._ids[k])
                if len(docs) >= n_results:
                    break
            return {"documents": [docs], "metadatas": [metas],
                    "distances": [dists], "ids": [ids]}

    # ---- одноразовая миграция из chroma.sqlite3 ----
    def migrate_from_chroma(self, chroma_db: str) -> int:
        import sqlite3
        if self.count() > 0 or not os.path.exists(chroma_db):
            return 0
        try:
            src = sqlite3.connect(f"file:{chroma_db}?mode=ro", uri=True)
            rows = src.execute(
                "SELECT e.embedding_id, m.key, m.string_value, m.int_value, m.float_value "
                "FROM embedding_metadata m JOIN embeddings e ON m.id=e.id"
            ).fetchall()
            src.close()
        except Exception as exc:
            log.warning("[MemPalace] chroma read for migration failed: %s", exc)
            return 0
        grouped: dict[str, dict] = {}
        for emb_id, key, sval, ival, fval in rows:
            d = grouped.setdefault(emb_id, {})
            if sval not in (None, ""):
                d[key] = sval
            elif fval is not None:
                d[key] = fval
            elif ival is not None:
                d[key] = ival
        docs, ids, metas = [], [], []
        for emb_id, d in grouped.items():
            doc = str(d.get("chroma:document", "") or "")
            if not doc:
                continue
            ids.append(emb_id); docs.append(doc)
            metas.append({
                "wing": d.get("wing", ""), "room": d.get("room", ""),
                "source": d.get("source", ""), "source_file": d.get("source_file", ""),
                "entity_id": d.get("entity_id", ""),
                "importance": d.get("importance", 3.0),
                "ts": d.get("ts", 0), "filed_at": d.get("filed_at", ""),
            })
        if not ids:
            return 0
        # пакетная вставка одной транзакцией (быстро)
        with self._lock:
            buf = []
            for doc, _id, meta in zip(docs, ids, metas):
                v = self._vec(doc)
                buf.append((
                    _id, doc, str(meta.get("wing", "")), str(meta.get("room", "")),
                    str(meta.get("source", "")), str(meta.get("source_file", "")),
                    str(meta.get("entity_id", "")),
                    float(meta.get("importance", 3.0) or 3.0),
                    int(meta.get("ts", 0) or 0), str(meta.get("filed_at", "")),
                    v.tobytes(),
                ))
            self._db.executemany(
                "INSERT OR REPLACE INTO drawers(id,document,wing,room,source,"
                "source_file,entity_id,importance,ts,filed_at,emb) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)", buf,
            )
            self._db.commit()
            self._load()
        return len(buf)


_init_lock = threading.Lock()
_bridge_ready = False
_bridge_error = ""
_config = None
_collection = None
_search_memories = None
_init_attempt_time: float = 0.0
_INIT_COOLDOWN_SEC = int(os.getenv("ARGOS_MEMPALACE_INIT_COOLDOWN_SEC", "300"))


def _ensure_sys_path() -> bool:
    if not _MEMPALACE_SRC.exists():
        return False
    path_str = str(_MEMPALACE_SRC)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)
    return True


def _ensure_init() -> bool:
    global _bridge_ready, _bridge_error, _config, _collection, _search_memories, _init_attempt_time
    if _bridge_ready:
        return True
    if not _MEMPALACE_ENABLED:
        return False
    if not _bridge_ready and _init_attempt_time > 0:
        elapsed = time.time() - _init_attempt_time
        if elapsed < _INIT_COOLDOWN_SEC:
            return False

    with _init_lock:
        if _bridge_ready:
            return True
        _init_attempt_time = time.time()
        try:
            class _Cfg:
                palace_path = _PALACE_PATH
                collection_name = _COLLECTION
            _config = _Cfg()
            Path(_config.palace_path).mkdir(parents=True, exist_ok=True)
            # Своё надёжное хранилище (sqlite3+numpy) вместо нестабильного chromadb.
            _vec_db = str(Path(_config.palace_path) / "mempalace_vec.sqlite3")
            _collection = _VecStore(_vec_db, _DeterministicEmbedding())
            # Одноразовая миграция 31k+ записей из старого chroma.sqlite3.
            if _collection.count() == 0:
                chroma_db = str(Path(_config.palace_path) / "chroma.sqlite3")
                migrated = _collection.migrate_from_chroma(chroma_db)
                if migrated:
                    log.info("[MemPalace] мигрировано %s записей из chroma.sqlite3", migrated)
            _search_memories = None

            _bridge_ready = True
            _bridge_error = ""
            log.info("[MemPalace] connected (sqlite+numpy, deterministic): %s (%s drawers)",
                     _vec_db, _collection.count())
        except Exception as exc:
            _bridge_error = str(exc)
            _bridge_ready = False
            log.warning("[MemPalace] init error: %s", exc)
    return _bridge_ready


def _layer0() -> str:
    try:
        path = Path(_IDENTITY_PATH)
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return "I am ARGOS — autonomous AI ecosystem by Всеволод."


def _all_drawers(entity_id: str = "") -> tuple[list[str], list[dict[str, Any]]]:
    if not _ensure_init():
        return [], []
    try:
        where = {"entity_id": entity_id} if entity_id else None
        payload = _collection.get(where=where, include=["documents", "metadatas"])
        return payload.get("documents", []), payload.get("metadatas", [])
    except Exception as exc:
        log.debug("[MemPalace] get drawers error: %s", exc)
        return [], []


def _layer1(max_drawers: int = 12, max_chars: int = 2400, entity_id: str = "") -> str:
    docs, metas = _all_drawers(entity_id=entity_id)
    if not docs:
        return ""

    # Генерируемый вывод (мысли/синтез) НЕ подаём обратно в essential-память —
    # иначе сущности видят свои прошлые мысли как "факты" и зацикливаются.
    _CYCLIC_ROOMS = {"thought", "synthesis", "collective", "mood", "stream"}
    scored = []
    for doc, meta in zip(docs, metas):
        meta = meta or {}
        if str(meta.get("room", "")).lower() in _CYCLIC_ROOMS:
            continue
        importance = 3.0
        for key in ("importance", "weight", "emotional_weight"):
            value = meta.get(key) if isinstance(meta, dict) else None
            if value is not None:
                try:
                    importance = float(value)
                    break
                except (TypeError, ValueError):
                    pass
        scored.append((importance, meta, doc or ""))

    scored.sort(key=lambda item: item[0], reverse=True)
    lines = ["## ARGOS MEMORY [L1 — Essential]"]
    total = 0
    for _, meta, doc in scored[:max_drawers]:
        wing = meta.get("wing", "unknown")
        room = meta.get("room", "general")
        snippet = str(doc).strip().replace("\n", " ")
        if len(snippet) > 180:
            snippet = snippet[:177] + "..."
        ent = meta.get("entity_id", "")
        ent_suffix = f" @{ent}" if ent else ""
        line = f"  [{wing}/{room}]{ent_suffix} {snippet}"
        lines.append(line)
        total += len(line)
        if total >= max_chars:
            break
    return "\n".join(lines)


def _layer2(wing: str, max_drawers: int = 6, entity_id: str = "") -> str:
    if not _ensure_init():
        return ""
    try:
        where = {"wing": wing}
        if entity_id:
            where["entity_id"] = entity_id
        payload = _collection.get(where=where, include=["documents", "metadatas"])
    except Exception as exc:
        log.debug("[MemPalace] layer2 error: %s", exc)
        return ""

    docs = payload.get("documents", [])
    metas = payload.get("metadatas", [])
    if not docs:
        return ""

    lines = [f"## ARGOS MEMORY [L2 — {wing}]"]
    for doc, meta in zip(docs[:max_drawers], metas[:max_drawers]):
        snippet = str(doc).strip().replace("\n", " ")
        if len(snippet) > 200:
            snippet = snippet[:197] + "..."
        ent = meta.get("entity_id", "")
        ent_suffix = f" @{ent}" if ent else ""
        lines.append(f"  [{meta.get('room', '?')}] {snippet}{ent_suffix}")
    return "\n".join(lines)


def _simple_overlap_score(query: str, text: str) -> float:
    q_tokens = {tok for tok in query.lower().split() if tok}
    t_tokens = {tok for tok in text.lower().split() if tok}
    if not q_tokens or not t_tokens:
        return 0.0
    return len(q_tokens & t_tokens) / max(len(q_tokens), 1)


def _store_fallback_memory(
    text: str,
    wing: str,
    room: str,
    importance: float,
    source: str,
    entity_id: str,
) -> bool:
    try:
        _FALLBACK_JSONL.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "text": text.strip(),
            "wing": wing,
            "room": room,
            "importance": float(importance),
            "source": source,
            "source_file": source,
            "entity_id": entity_id or "",
            "filed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "ts": int(time.time()),
        }
        with _FALLBACK_JSONL.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return True
    except Exception as exc:
        log.debug("[MemPalace] fallback store error: %s", exc)
        return False


def _allow_fallback_memory(source: str, entity_id: str, room: str) -> bool:
    if entity_id:
        return True
    if room in {"thought", "synthesis", "collective"}:
        return True
    source_norm = (source or "").strip().lower()
    return source_norm in {"autogpt-entity", "entity-loop", "collective-loop", "local-safety", "codex-diagnostic"}


def _fallback_count() -> int:
    try:
        if not _FALLBACK_JSONL.exists():
            return 0
        with _FALLBACK_JSONL.open("r", encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip())
    except Exception:
        return 0


def _fallback_memory_hits(query: str, top_k: int = 5, wing: str = "", entity_id: str = "") -> list[dict[str, Any]]:
    if not _FALLBACK_JSONL.exists():
        return []
    try:
        lines = _FALLBACK_JSONL.read_text(encoding="utf-8").splitlines()[-2000:]
    except Exception as exc:
        log.debug("[MemPalace] fallback read error: %s", exc)
        return []

    ranked: list[dict[str, Any]] = []
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        text = str(item.get("text", "") or "")
        if not text:
            continue
        if entity_id and str(item.get("entity_id", "") or "") != entity_id:
            continue
        if wing and str(item.get("wing", "") or "") != wing:
            continue
        score = _simple_overlap_score(query, text) if query else float(item.get("importance", 0.0) or 0.0)
        ranked.append(
            {
                "text": text,
                "wing": str(item.get("wing", "")),
                "room": str(item.get("room", "")),
                "score": float(score),
                "source_file": str(item.get("source_file", item.get("source", "fallback-jsonl"))),
                "entity_id": str(item.get("entity_id", "") or entity_id),
            }
        )

    ranked.sort(key=lambda item: item["score"], reverse=True)
    return ranked[:top_k]


def search_memory(query: str, top_k: int = 5, wing: str = "", entity_id: str = "") -> list[dict[str, Any]]:
    if not query.strip():
        return []
    _touch_vec_db([])  # mark activity
    fallback_hits = _fallback_memory_hits(query, top_k=top_k, wing=wing, entity_id=entity_id)
    if not _ensure_init():
        return fallback_hits
    if entity_id:
        docs, metas = _all_drawers(entity_id=entity_id)
        ranked: list[dict[str, Any]] = []
        for doc, meta in zip(docs, metas):
            meta = meta or {}
            if wing and str(meta.get("wing", "") or "") != wing:
                continue
            score = _simple_overlap_score(query, str(doc))
            ranked.append(
                {
                    "text": str(doc),
                    "wing": str(meta.get("wing", "")),
                    "room": str(meta.get("room", "")),
                    "score": float(score),
                    "source_file": str(meta.get("source_file", "")),
                    "entity_id": str(meta.get("entity_id", "") or entity_id),
                }
            )
        ranked.extend(fallback_hits)
        ranked.sort(key=lambda item: item["score"], reverse=True)
        return ranked[:top_k]
    try:
        where = {"wing": wing} if wing else None
        result = _collection.query(query_texts=[query], n_results=top_k, where=where)
    except Exception as exc:
        log.debug("[MemPalace] search error: %s", exc)
        return fallback_hits

    docs = (result.get("documents") or [[]])[0]
    metas = (result.get("metadatas") or [[]])[0]
    dists = (result.get("distances") or [[]])[0]
    out = []
    for doc, meta, dist in zip(docs, metas, dists):
        meta = meta or {}
        if entity_id and str(meta.get("entity_id", "") or "") != entity_id:
            continue
        out.append(
            {
                "text": str(doc),
                "wing": str(meta.get("wing", "")),
                "room": str(meta.get("room", "")),
                "score": float(1.0 - dist),  # cosine similarity
                "source_file": str(meta.get("source_file", "")),
                "entity_id": str(meta.get("entity_id", "")),
            }
        )
    out.extend(fallback_hits)
    out.sort(key=lambda item: item["score"], reverse=True)
    return out[:top_k]


def store_memory(
    text: str,
    wing: str = "technical",
    room: str = "general",
    importance: float = 3.0,
    source: str = "argos",
    entity_id: str = "",
) -> bool:
    if not _ensure_init():
        if _allow_fallback_memory(source, entity_id, room):
            return _store_fallback_memory(text, wing, room, importance, source, entity_id)
        return False
    if not text or not text.strip():
        return False
    try:
        # Детерминированный ID по содержимому (БЕЗ time.time()) — один и тот же
        # текст/источник = один drawer. Иначе при ре-синхронизации копятся дубли.
        text_clean = text.strip()
        drawer_id = hashlib.sha256(
            f"{wing}::{room}::{source}::{entity_id}::{text_clean}".encode("utf-8")
        ).hexdigest()[:24]
        metadata = {
            "wing": wing,
            "room": room,
            "importance": float(importance),
            "source": source,
            "source_file": source,
            "entity_id": entity_id or "",
            "filed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "ts": int(time.time()),
        }
        # upsert: если drawer с таким id уже есть — обновит, а не создаст дубль
        try:
            _collection.upsert(documents=[text_clean], ids=[drawer_id], metadatas=[metadata])
        except AttributeError:
            # старый chromadb без upsert — эмулируем
            try:
                _collection.delete(ids=[drawer_id])
            except Exception:
                pass
            _collection.add(documents=[text_clean], ids=[drawer_id], metadatas=[metadata])
        return True
    except Exception as exc:
        log.warning("[MemPalace] store error: %s", exc)
        if _allow_fallback_memory(source, entity_id, room):
            return _store_fallback_memory(text, wing, room, importance, source, entity_id)
        return False


def get_memory_context(query: str = "", wing: str = "", entity_id: str = "") -> str:
    parts: list[str] = []
    identity = _layer0()
    if identity:
        parts.append(identity)

    # Vertex AI Search RAG — семантическая память (приоритет, если есть запрос)
    if query and os.getenv("ARGOS_RAG_ENABLED", "1").strip() in {"1", "true", "on"}:
        try:
            from src.argos_rag import rag_context
            rc = rag_context(query, top_k=4)
            if rc:
                parts.append(rc)
        except Exception:
            pass

    essential = _layer1(entity_id=entity_id)
    if essential:
        parts.append(essential)

    if wing:
        on_demand = _layer2(wing, entity_id=entity_id)
        if on_demand:
            parts.append(on_demand)

    if query:
        hits = search_memory(query, top_k=3, wing=wing, entity_id=entity_id)
        if hits:
            lines = ["## ARGOS MEMORY [L3 — Search results]"]
            for hit in hits:
                snippet = hit["text"].replace("\n", " ")
                if len(snippet) > 200:
                    snippet = snippet[:197] + "..."
                ent = hit.get("entity_id", "")
                ent_suffix = f" @{ent}" if ent else ""
                lines.append(f"  [{hit['wing']}/{hit['room']}] (score={hit['score']:.3f}){ent_suffix} {snippet}")
            parts.append("\n".join(lines))

    return "\n\n".join(part for part in parts if part).strip()


def status() -> str:
    if not _ensure_init():
        fallback = _fallback_count()
        if not _MEMPALACE_ENABLED:
            return f"⚫ MemPalace: отключён (ARGOS_MEMPALACE=0)\n  Fallback JSONL: {fallback}"
        return f"🔴 MemPalace: недоступен ({_bridge_error or 'init failed'})\n  Fallback JSONL: {fallback}"

    try:
        count = _collection.count()
        fallback = _fallback_count()
        _, metas = _all_drawers()
        wings: dict[str, int] = {}
        entities: dict[str, int] = {}
        for meta in metas:
            wing = str((meta or {}).get("wing", "?"))
            wings[wing] = wings.get(wing, 0) + 1
            ent = str((meta or {}).get("entity_id", "") or "")
            if ent:
                entities[ent] = entities.get(ent, 0) + 1
        wing_str = "  ".join(f"{name}:{value}" for name, value in sorted(wings.items())) or "empty"
        ent_str = "  ".join(f"{name}:{value}" for name, value in sorted(entities.items())) or "shared"
        return (
            f"🧠 *MemPalace*\n"
            f"  Drawers: {count}\n"
            f"  Wings:   {wing_str}\n"
            f"  Entities:{(' ' + ent_str) if ent_str else ' shared'}\n"
            f"  Fallback JSONL: {fallback}\n"
            f"  Path:    `{_config.palace_path}`\n"
            f"  Mode:    `sqlite+numpy/deterministic`"
        )
    except Exception as exc:
        return f"🔴 MemPalace: ошибка статуса ({exc})\n  Fallback JSONL: {_fallback_count()}"
