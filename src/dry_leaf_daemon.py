"""
dry_leaf_daemon.py — MemPalace Cold Data Offloader

Сканирует mempalace_vec.sqlite3 и memory.db, находит «холодные» векторы
(importance_score < порог, last_access_ts > N дней), выгружает в S3/LFS,
освобождает место на диске.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

log = logging.getLogger("argos.dry_leaf")

# ── Конфигурация ──────────────────────────────────────────────
REPO_ROOT = Path(os.getenv("ARGOS_REPO_ROOT", Path(__file__).resolve().parent.parent))
PALACE_DB = REPO_ROOT / "data" / "mempalace" / "mempalace_vec.sqlite3"
MEMORY_DB = REPO_ROOT / "data" / "memory.db"
OFFLOAD_DIR = Path(os.getenv("DRY_LEAF_OFFLOAD_DIR", str(REPO_ROOT / "data" / "dry_leaf_archive")))

# Пороги
IMPORTANCE_THRESHOLD = float(os.getenv("DRY_LEAF_IMPORTANCE_THRESHOLD", "0.3"))
ACCESS_AGE_DAYS = int(os.getenv("DRY_LEAF_ACCESS_AGE_DAYS", "30"))
MAX_BATCH_SIZE = int(os.getenv("DRY_LEAF_MAX_BATCH", "500"))
DISK_FREE_THRESHOLD_PCT = float(os.getenv("DRY_LEAF_DISK_FREE_PCT", "15.0"))


class DryLeafDaemon:
    """Сканирует и выгружает холодные векторы."""

    def __init__(self, dry_run: bool = True):
        self.dry_run = dry_run
        self.stats = {"scanned": 0, "cold": 0, "offloaded": 0, "freed_kb": 0}
        self._ensure_schema()

    # ── Миграция схемы ───────────────────────────────────────
    def _ensure_schema(self) -> None:
        """Добавляет last_access_ts и importance_score, если их нет."""
        for db_path in [PALACE_DB, MEMORY_DB]:
            if not db_path.exists():
                continue
            conn = sqlite3.connect(str(db_path))
            try:
                # Определяем таблицы для миграции
                if db_path.name == "mempalace_vec.sqlite3":
                    tables = ["drawers"]
                else:
                    tables = ["facts", "notes"]

                for table in tables:
                    cols = [c[1] for c in conn.execute(f"PRAGMA table_info({table})")]
                    if "last_access_ts" not in cols:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN last_access_ts REAL DEFAULT 0.0")
                        log.info("Added last_access_ts to %s.%s", db_path.name, table)
                    if "importance_score" not in cols:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN importance_score REAL DEFAULT 0.5")
                        log.info("Added importance_score to %s.%s", db_path.name, table)

                conn.commit()
            except Exception as e:
                log.warning("Schema migration failed for %s: %s", db_path.name, e)
            finally:
                conn.close()

    # ── Обновление метрик доступа ────────────────────────────
    def touch(self, db_key: str, table: str = "drawers") -> None:
        """Обновить last_access_ts при обращении к вектору."""
        db_path = PALACE_DB if table == "drawers" else MEMORY_DB
        if not db_path.exists():
            return
        conn = sqlite3.connect(str(db_path))
        try:
            col = "id" if table == "drawers" else ("id" if table in ("facts", "notes") else "key")
            conn.execute(
                f"UPDATE {table} SET last_access_ts = ? WHERE {col} = ?",
                (time.time(), db_key),
            )
            conn.commit()
        except Exception:
            pass
        finally:
            conn.close()

    # ── Сканирование холода ──────────────────────────────────
    def scan_cold(self) -> list[dict]:
        """Возвращает список холодных векторов для выгрузки."""
        cold_items = []
        cutoff_ts = (datetime.now() - timedelta(days=ACCESS_AGE_DAYS)).timestamp()

        for db_path, table in [(PALACE_DB, "drawers"), (MEMORY_DB, "facts"), (MEMORY_DB, "notes")]:
            if not db_path.exists():
                continue
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    f"""SELECT * FROM {table}
                        WHERE (last_access_ts = 0 OR last_access_ts < ?)
                        AND importance_score < ?
                        LIMIT {MAX_BATCH_SIZE}""",
                    (cutoff_ts, IMPORTANCE_THRESHOLD),
                ).fetchall()
                for row in rows:
                    cold_items.append({
                        "source_db": str(db_path),
                        "table": table,
                        "row_id": row["id"],
                        "data": dict(row),
                    })
                self.stats["scanned"] += len(rows) if rows else 0
            except Exception as e:
                log.warning("Scan failed for %s.%s: %s", db_path.name, table, e)
            finally:
                conn.close()

        self.stats["cold"] = len(cold_items)
        return cold_items

    # ── Выгрузка в архив ─────────────────────────────────────
    def offload(self, items: list[dict]) -> int:
        """Экспортирует холодные векторы в OFFLOAD_DIR и удаляет из БД."""
        OFFLOAD_DIR.mkdir(parents=True, exist_ok=True)
        batch_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        archive_path = OFFLOAD_DIR / f"dry_leaf_{batch_id}.jsonl"
        freed_kb = 0

        with open(archive_path, "w", encoding="utf-8") as f:
            for item in items:
                f.write(json.dumps(item["data"], ensure_ascii=False, default=str) + "\n")
                freed_kb += len(json.dumps(item["data"], default=str)) // 1024 + 1

        if not self.dry_run:
            # Группируем по БД и таблице для batch-удаления
            deleted = {}
            for item in items:
                key = (item["source_db"], item["table"])
                deleted.setdefault(key, []).append(item["row_id"])

            for (db_path, table), ids in deleted.items():
                conn = sqlite3.connect(db_path)
                try:
                    placeholders = ",".join("?" * len(ids))
                    conn.execute(f"DELETE FROM {table} WHERE id IN ({placeholders})", ids)
                    conn.commit()
                    log.info("Deleted %d rows from %s.%s", len(ids), Path(db_path).name, table)
                except Exception as e:
                    log.error("Delete failed for %s.%s: %s", Path(db_path).name, table, e)
                    conn.rollback()
                finally:
                    conn.close()

        self.stats["offloaded"] = len(items)
        self.stats["freed_kb"] = freed_kb
        return len(items)

    # ── Проверка дискового пространства ──────────────────────
    def should_run(self) -> bool:
        """True если диск заполнен выше порога."""
        try:
            import shutil
            total, used, free = shutil.disk_usage(str(REPO_ROOT))
            free_pct = free / total * 100
            return free_pct < DISK_FREE_THRESHOLD_PCT
        except Exception:
            return False

    # ── Основной цикл ────────────────────────────────────────
    def run_once(self) -> dict:
        """Один цикл сканирования и выгрузки."""
        if not self.should_run():
            return {"status": "skip", "reason": "disk_free_ok", "stats": self.stats}

        cold = self.scan_cold()
        if not cold:
            return {"status": "ok", "reason": "no_cold_data", "stats": self.stats}

        count = self.offload(cold)

        try:
            import shutil
            total, _, free = shutil.disk_usage(str(REPO_ROOT))
            free_pct = free / total * 100
        except Exception:
            free_pct = 0

        return {
            "status": "offloaded" if not self.dry_run else "dry_run",
            "count": count,
            "freed_kb": self.stats["freed_kb"],
            "disk_free_pct": round(free_pct, 1),
            "stats": dict(self.stats),
        }

    def status(self) -> dict:
        return {
            "daemon": "dry_leaf",
            "thresholds": {
                "importance": IMPORTANCE_THRESHOLD,
                "age_days": ACCESS_AGE_DAYS,
                "disk_free_pct": DISK_FREE_THRESHOLD_PCT,
            },
            "storage": {"palace_db": str(PALACE_DB), "memory_db": str(MEMORY_DB), "archive": str(OFFLOAD_DIR)},
            "stats": dict(self.stats),
        }


# ── CLI ─────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    dry_run = "--apply" not in sys.argv
    if dry_run:
        log.info("DRY RUN mode — use --apply to actually offload")

    daemon = DryLeafDaemon(dry_run=dry_run)
    result = daemon.run_once()
    log.info("Result: %s", json.dumps(result, indent=2, default=str))
