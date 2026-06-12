"""
argos_disk_cleaner.py — безопасная LRU-ротация логов/кэша при дефиците диска.

Контекст: диск Orion ~78% занят, MemPalace растёт (~40k drawers). При заполнении
до 100% SQLite/логи падают с "database or disk is full". Модуль вычищает старый
кэш/логи, НЕ трогая БД, модели и системные пути.

Собран из итеративного ревью в Telegram-сессии 2026-06-05 с ИСПРАВЛЕНИЕМ всех
найденных там багов:
  1. `def init` → `__init__` (класс был нерабочий).
  2. `st_atime` → `st_mtime` (atime ненадёжен при noatime mount).
  3. Нет защиты критфайлов → PROTECTED_PATTERNS (*.db/*.sqlite/mempalace*/ключи)
     + PROTECTED_DIRS (/proc,/sys,/etc,...).
  4. `logging.getLogger(name)` → `__name__`.
  5. Двойной syscall get_free_space на итерации → periodic check_interval.
  6. `max_file_size_mb` инвертированная семантика → явный `min_file_size_mb`
     (целимся в КРУПНЫЕ старые файлы — меньше I/O-операций).
  7. `_get_mount_device` возвращал 0 (коллизия с реальным st_dev) → -1.
  8. Группировка по st_dev объявлена но не использовалась → _group_dirs_by_device.
  9. TOCTOU между stat и unlink → O_NOFOLLOW + fstat-сверка inode перед unlink.
 10. bool-результат неинформативен → CleanResult enum (OK/NO_CANDIDATES/...).
 11. Демон без остановки → threading.Event + graceful stop.
 12. shutil.disk_usage → os.statvfs (f_bavail — реальное место для не-root, учёт 5%
     reserved в ext4).
 13. Directory-traversal (../) → os.path.abspath нормализация в is_protected_dir.

Безопасный дефолт: dry_run=True (ничего не удаляет, только логирует).
"""
from __future__ import annotations

import fnmatch
import logging
import os
import threading
import time
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    from src.argos_logger import get_logger
    logger = get_logger("argos.disk_cleaner")
except Exception:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s - [DISK_CLEANER] - %(levelname)s - %(message)s")
    logger = logging.getLogger(__name__)  # ФИКС #4

# ── Защита ──────────────────────────────────────────────────────────────────
PROTECTED_PATTERNS: List[str] = [
    "*.db", "*.sqlite", "*.sqlite-wal", "*.sqlite-shm",
    "*.pid", "*.sock", "*.lock",
    "argos-v1*", "mistral*", "mempalace*",   # модели + память
    "cleaner.log",
    "*.conf", "*.yaml", "*.yml", "*.json", "*.env",
    "*.key", "*.pem", "*.crt",               # секреты/сертификаты
]
PROTECTED_DIRS: List[str] = [
    "/proc", "/sys", "/dev", "/run", "/etc", "/boot", "/root",
]


def is_protected(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in PROTECTED_PATTERNS)


def is_protected_dir(path) -> bool:
    """ФИКС #13: abspath-нормализация против ../ traversal."""
    try:
        abs_path = os.path.abspath(str(path))
        return any(abs_path == d or abs_path.startswith(d + os.sep)
                   for d in PROTECTED_DIRS)
    except Exception:
        return True  # безопасный дефолт


def get_free_gb(path) -> float:
    """ФИКС #12: os.statvfs, f_bavail — место, реально доступное не-root."""
    try:
        sv = os.statvfs(str(path))
        return (sv.f_bavail * sv.f_frsize) / (1024 ** 3)
    except (OSError, AttributeError):
        # Windows fallback
        import shutil
        try:
            return shutil.disk_usage(str(path)).free / (1024 ** 3)
        except Exception as e:
            logger.error("disk_usage %s: %s", path, e)
            return 0.0


class CleanResult(Enum):
    OK = "SUCCESS"                              # очистка прошла
    NO_CANDIDATES = "SPACE_LOW_NO_CANDIDATES"   # места мало, но всё защищено
    INSUFFICIENT = "SPACE_LOW_AFTER_CLEANUP"    # удалили всё доступное, мало
    SKIPPED = "SPACE_OK_SKIPPED"               # очистка не требовалась


class ArgosDiskCleaner:
    def __init__(                              # ФИКС #1: __init__
        self,
        target_dirs: List[str],
        min_free_gb: float = 50.0,
        dry_run: bool = True,                  # БЕЗОПАСНЫЙ дефолт
        max_file_age_hours: float = 24.0,
        min_file_size_mb: Optional[float] = 10.0,   # ФИКС #6: целимся в КРУПНЫЕ
        check_interval: int = 5,
    ):
        self.target_dirs: List[Path] = []
        for d in target_dirs:
            p = Path(d)
            if is_protected_dir(p):
                logger.error("Директория системная/защищённая, игнорируем: %s", d)
                continue
            if p.exists() and p.is_dir():
                self.target_dirs.append(p)
            else:
                logger.warning("Директория не найдена: %s", d)
        self.min_free_gb = min_free_gb
        self.dry_run = dry_run
        self.max_file_age_hours = max_file_age_hours
        self.min_file_size_mb = min_file_size_mb
        self.check_interval = max(1, check_interval)
        if dry_run:
            logger.info("DRY_RUN активен — файлы НЕ удаляются, только лог.")

    def _get_mount_device(self, path: Path) -> int:
        try:
            return os.stat(path).st_dev
        except Exception as e:
            logger.error("st_dev для %s: %s", path, e)
            return -1  # ФИКС #7

    def _group_dirs_by_device(self) -> Dict[int, List[Path]]:
        """ФИКС #8: одна ФС — один проход, без дублей."""
        groups: Dict[int, List[Path]] = {}
        for d in self.target_dirs:
            dev = self._get_mount_device(d)
            if dev == -1:
                continue
            groups.setdefault(dev, []).append(d)
        return groups

    def _collect_candidates(self, dirs: List[Path]) -> List[dict]:
        candidates: List[dict] = []
        now = time.time()
        max_age = self.max_file_age_hours * 3600
        min_size = (self.min_file_size_mb * 1024 * 1024
                    if self.min_file_size_mb else 0)

        def _scan(dir_path: Path):
            try:
                with os.scandir(dir_path) as it:
                    for entry in it:
                        try:
                            if entry.is_symlink():     # симлинки не трогаем
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                child = Path(entry.path)
                                if not is_protected_dir(child):
                                    _scan(child)
                                continue
                            if not entry.is_file(follow_symlinks=False):
                                continue
                            if is_protected(entry.name):
                                continue
                            st = entry.stat(follow_symlinks=False)
                            if st.st_size < min_size:           # ФИКС #6
                                continue
                            if (now - st.st_mtime) < max_age:   # ФИКС #2: mtime
                                continue
                            candidates.append({
                                "path": entry.path,
                                "mtime": st.st_mtime,
                                "size": st.st_size,
                                "inode": st.st_ino,             # ФИКС #9
                            })
                        except (FileNotFoundError, PermissionError, OSError):
                            continue
            except (FileNotFoundError, PermissionError, OSError):
                return

        for d in dirs:
            _scan(d)
        candidates.sort(key=lambda x: x["mtime"])   # старые первыми
        return candidates

    def _safe_unlink(self, path: str, expected_inode: int) -> bool:
        """ФИКС #9: O_NOFOLLOW + fstat-сверка inode перед unlink (TOCTOU)."""
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except (FileNotFoundError, OSError) as e:
            logger.debug("open %s: %s", path, e)
            return False
        try:
            if os.fstat(fd).st_ino != expected_inode:
                logger.warning("TOCTOU: %s подменён (inode), пропуск", path)
                return False
            os.unlink(path)
            logger.info("Удалён: %s", path)
            return True
        except (FileNotFoundError, PermissionError, OSError) as e:
            logger.debug("unlink %s: %s", path, e)
            return False
        finally:
            os.close(fd)

    def execute(self) -> CleanResult:
        if not self.target_dirs:
            logger.error("Нет валидных директорий.")
            return CleanResult.INSUFFICIENT
        result = CleanResult.SKIPPED
        for dev, dirs in self._group_dirs_by_device().items():
            rep = dirs[0]
            free = get_free_gb(rep)
            if free >= self.min_free_gb:
                logger.info("[FS %s] %.2f ГБ свободно — норма.", dev, free)
                continue
            logger.warning("[FS %s] дефицит (%.2f ГБ), ротация.", dev, free)
            candidates = self._collect_candidates(dirs)
            if not candidates:
                logger.error("[FS %s] места мало, но всё защищено фильтрами.", dev)
                if result != CleanResult.INSUFFICIENT:
                    result = CleanResult.NO_CANDIDATES
                continue
            for idx, fi in enumerate(candidates):
                if idx > 0 and idx % self.check_interval == 0:   # ФИКС #5
                    if get_free_gb(rep) >= self.min_free_gb:
                        logger.info("[FS %s] норма восстановлена.", dev)
                        break
                if self.dry_run:
                    logger.info("[DRY_RUN] кандидат: %s (%.2f МБ)",
                                fi["path"], fi["size"] / 1024**2)
                    result = CleanResult.OK
                    continue
                if self._safe_unlink(fi["path"], fi["inode"]):
                    result = CleanResult.OK
            if get_free_gb(rep) < self.min_free_gb:
                result = CleanResult.INSUFFICIENT
        return result


def start_cleaner_daemon(cleaner: ArgosDiskCleaner, interval_seconds: int = 900,
                         stop_event: Optional[threading.Event] = None
                         ) -> Tuple[threading.Thread, threading.Event]:
    """ФИКС #11: демон с graceful shutdown через Event."""
    stop_event = stop_event or threading.Event()

    def _worker():
        logger.info("Демон очистки диска: скан каждые %dс", interval_seconds)
        while not stop_event.is_set():
            try:
                logger.info("Итерация: %s", cleaner.execute().value)
            except Exception as e:
                logger.error("Сбой цикла очистки: %s", e)
            if stop_event.wait(timeout=interval_seconds):
                break
        logger.info("Демон очистки остановлен.")

    t = threading.Thread(target=_worker, daemon=True, name="ArgosDiskCleaner")
    t.start()
    return t, stop_event


if __name__ == "__main__":
    import tempfile
    c = ArgosDiskCleaner([tempfile.gettempdir()], min_free_gb=999999, dry_run=True)
    print("результат:", c.execute().value)
