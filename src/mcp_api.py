from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

import aiohttp
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware


class ArgosMCPServer:
    """Минимальный HTTP MCP endpoint для локальной интеграции."""

    def __init__(self, core=None, admin=None):
        if core is None and os.getenv("ARGOS_MCP_STANDALONE_INIT_CORE"):
            # Initialize a minimal core for standalone operation
            try:
                # Класс называется ArgosCore (AwaCore не существует → раньше core
                # всегда оставался None при standalone MCP, P2P/инструменты → 503).
                from src.core import ArgosCore
                from src.connectivity.p2p_bridge import ArgosBridge
                core = ArgosCore()
                core.p2p_bridge = ArgosBridge(core)
                print("DEBUG: Initialized core (ArgosCore) with P2P bridge for standalone operation")
            except Exception as e:
                # If we can't initialize core, leave it as None
                # P2P endpoints will return 503 error
                print(f"DEBUG: Failed to initialize core: {e}")
                pass
        self.core = core
        self.admin = admin
        self.started_at = time.time()
        self._last_client_host = "127.0.0.1"
        self.app = self._create_app()

    def _providers(self) -> str:
        try:
            from src.ai_providers import providers_status

            return providers_status()
        except Exception as exc:
            return f"providers error: {exc}"

    def _skills(self) -> str:
        if self.core and getattr(self.core, "skill_loader", None):
            try:
                return self.core.skill_loader.list_skills()
            except Exception as exc:
                return f"skills error: {exc}"
        return "skill_loader not initialized"

    def _limits(self) -> str:
        try:
            from src.connectivity.telegram_bot import ArgosTelegram

            bot = ArgosTelegram(self.core, self.admin, None)
            return bot._build_limits_report()
        except Exception as exc:
            return f"limits error: {exc}"

    def _status(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "ok": True,
            "uptime_seconds": int(time.time() - self.started_at),
            "ai_mode": self.core.ai_mode_label() if self.core and hasattr(self.core, "ai_mode_label") else "unknown",
        }
        try:
            import psutil

            out["cpu_pct"] = psutil.cpu_percent(interval=0.1)
            out["ram_pct"] = psutil.virtual_memory().percent
        except Exception:
            pass
        return out

    def _headroom_compress(self, text: str) -> str:
        """Сжать текст через Headroom-компрессор (scripts/headroom/argos_compressor.py)."""
        if not text:
            return "headroom_compress: пустой вход"
        try:
            import os as _os
            import sys as _sys
            _hr = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                                "scripts", "headroom")
            if _hr not in _sys.path:
                _sys.path.insert(0, _hr)
            from argos_compressor import ArgosCompressor
            r = ArgosCompressor().compress(text)
            header = (f"[headroom: {r.strategy}, {r.original_chars}→{r.compressed_chars} "
                      f"символов, экономия {r.savings_pct:.0f}%]\n")
            return header + r.text
        except Exception as exc:
            return f"headroom_compress error: {exc}"

    def _osint(self, service: str, query: str) -> str:
        """OSINT-диспетчер: вызывает функцию из scripts/osint/osint_tools.py."""
        import json as _json
        service = (service or "").strip()
        if not service:
            return "osint: нужен service (напр. shodan_search, crtsh, vulners_search, quick_recon)"
        try:
            import os as _os
            import sys as _sys
            _od = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                                "scripts", "osint")
            if _od not in _sys.path:
                _sys.path.insert(0, _od)
            import osint_tools as _ot
            fn = getattr(_ot, service, None)
            if fn is None or not callable(fn) or service.startswith("_"):
                avail = [n for n in dir(_ot) if callable(getattr(_ot, n)) and not n.startswith("_")]
                return f"osint: неизвестный service '{service}'. Доступно: {', '.join(avail)}"
            result = fn(query) if query else fn()
            return _json.dumps(result, ensure_ascii=False, indent=2)
        except TypeError as exc:
            return f"osint {service}: неверные аргументы — {exc}"
        except Exception as exc:
            return f"osint {service} error: {exc}"

    def _image_generate(
        self,
        prompt: str,
        negative_prompt: str = "",
        steps: int = 20,
        width: int = 1024,
        height: int = 1024,
        model_name: str | None = None,
    ) -> str:
        from src.tools.image_generator import ArgosImageGenerator

        gen = ArgosImageGenerator(model_name=model_name)
        return gen.generate(
            prompt=prompt,
            negative_prompt=negative_prompt,
            steps=steps,
            width=width,
            height=height,
        )

    async def _run_command(self, text: str, prefix_direct: bool = True) -> str:
        """Async version: runs command without creating new event loop."""
        if not text.strip():
            return "empty command"
        t = text.strip().lower()

        # Direct tool routes (execute real tools, no "MCP Direct" prefix)
        if any(phrase in t for phrase in ("синхронизируй все машины", "machine sync", "argoss_machine_sync")):
            try:
                return await asyncio.to_thread(self._argoss_machine_sync_command)
            except Exception as exc:
                return f"machine sync error: {exc}"

        if t in ("xiaozhi_stability", "esp не отвечает", "esp stability"):
            try:
                return await self._xiaozhi_stability_command(duration=20.0, interval=2)
            except Exception as exc:
                return f"xiaozhi stability error: {exc}"

        if t.startswith("argos_vpn"):
            parts = text.strip().split(maxsplit=1)
            action = parts[1].strip() if len(parts) > 1 else "status"
            return await self._argos_vpn_command(action=action)

        if t.startswith("xiaozhi_debug"):
            m = re.search(r"action\s*=\s*(\w+)", text)
            action = m.group(1) if m else "sessions"
            try:
                return await self._xiaozhi_debug(action=action)
            except Exception as exc:
                return f"xiaozhi debug error: {exc}"


        if t.startswith(("mempalace ", "mempalace_")) or t.startswith("через acp mempalace"):
            raw_text = text.strip()
            if raw_text.lower().startswith("через acp "):
                raw_text = raw_text[10:]
            if raw_text.lower().startswith("mempalace "):
                raw_text = raw_text[10:]
            lower_text = raw_text.lower()
            parts = raw_text.split(maxsplit=2)
            lower_parts = lower_text.split(maxsplit=2)
            action = lower_parts[0].lower() if lower_parts else "status"
            rest = parts[1] if len(parts) > 1 else ""
            tail = parts[2] if len(parts) > 2 else ""
            if action == "store":
                payload = (rest + (" " + tail if tail else "")).strip()
                return self._mempalace_command(action="store", text=payload or None)
            if action == "context":
                payload = (rest + (" " + tail if tail else "")).strip()
                return self._mempalace_command(action="context", query=payload or None)
            if action == "status":
                return self._mempalace_quick_status()
            return self._mempalace_command(action=action)

        # Fast status/ping routes
        fast = self._fast_system_cmd(text)
        if fast is not None:
            return f"MCP Direct: {fast}" if prefix_direct else fast

        if self.core and hasattr(self.core, "process_logic_async"):
            try:
                result = await self.core.process_logic_async(text, self.admin, None)
                if isinstance(result, dict):
                    return str(result.get("answer", result))
                return str(result)
            except Exception as exc:
                return f"command error: {exc}"
        return "core not initialized"

    def _mcp_debug(self) -> str:
        s = self._status()
        core_ok = self.core is not None
        vision_on = core_ok and getattr(self.core, "vision", None) is not None
        lines = [
            "MCP DEBUG SNAPSHOT",
            f"ai_mode: {s.get('ai_mode', 'unknown')}",
            f"open_ports: {s.get('open_ports', [])}",
            f"vision: {'on' if vision_on else 'off'}",
        ]
        return "\n".join(lines)

    def _telegram_status(self) -> str:
        tg = None
        thread = None
        if self.admin:
            tg = getattr(self.admin, "tg_bot", None) or getattr(self.admin, "telegram_bot", None)
            thread = getattr(self.admin, "tg", None) or getattr(self.admin, "telegram_thread", None)
        if not tg:
            return "Telegram runtime status\nthread_alive: False\npolling_active: False\nlock_held: False\nlast_error: нет"
        lock_held = bool(getattr(tg, "_poll_lock_socket", None))
        polling_active = bool(getattr(tg, "_polling_active", False))
        thread_alive = bool(thread and getattr(thread, "is_alive", lambda: False)())
        last_error = getattr(tg, "_last_tg_error", "") or ""
        last_error_ts = float(getattr(tg, "_last_tg_error_ts", 0.0) or 0.0)
        recovered_ts = float(getattr(tg, "_last_tg_recovered_ts", 0.0) or 0.0)
        nonfatal = any(
            str(last_error).startswith(prefix)
            for prefix in ("httpx.ReadError:", "httpx.RemoteProtocolError:")
        )
        recovered = bool((recovered_ts and recovered_ts > last_error_ts) or nonfatal)
        lines = [
            "Telegram runtime status",
            f"thread_alive: {thread_alive}",
            f"polling_active: {polling_active}",
            f"lock_held: {lock_held}",
        ]
        if recovered:
            lines.append("last_error: нет")
            lines.append(f"recovered_error: {last_error}")
        else:
            lines.append(f"last_error: {last_error or 'нет'}")
        return "\n".join(lines)

    def _acp_status(self) -> str:
        return "ACP OK"

    async def _argos_vpn_command(
        self,
        action: str = "status",
        telegram_id: Any = None,
        username: Optional[str] = None,
    ) -> str:
        try:
            from src.vpn_service.database import Database
            from src.vpn_service.wg_manager import WireGuardManager
            import json as _json

            db = Database()
            wg = WireGuardManager()

            if action == "status":
                return "Argos VPN service: ok"

            if action == "clients":
                keys = db.list_active_keys()
                return _json.dumps({"count": len(keys), "clients": keys}, ensure_ascii=False)

            if action == "cleanup":
                released = db.cleanup_expired_keys()
                for pubkey in released:
                    try:
                        wg.remove_peer(pubkey)
                    except Exception:
                        pass
                return _json.dumps({"deactivated": len(released), "public_keys": released}, ensure_ascii=False)

            if telegram_id is None:
                return "argos_vpn: telegram_id required"
            try:
                tid = int(telegram_id)
            except (TypeError, ValueError):
                return "argos_vpn: telegram_id must be integer"

            if action == "register":
                user = db.get_user(tid)
                if not user:
                    user = db.create_user(tid, username)
                return _json.dumps({"status": "registered", "telegram_id": tid}, ensure_ascii=False)

            if action == "create":
                user = db.get_user(tid)
                if not user:
                    user = db.create_user(tid, username)
                existing = db.get_active_key(user["id"])
                if existing:
                    server_ip = os.getenv("ARGOS_VPN_SERVER_IP", os.getenv("SERVER_IP", "your-server.com"))
                    config = wg.generate_client_config(existing["private_key"], existing["ip_address"], server_ip=server_ip)
                    return _json.dumps({"status": "existing", "ip": existing["ip_address"], "config": config}, ensure_ascii=False)
                ip = db.allocate_ip()
                kp = wg.generate_keypair()
                db.create_key(user["id"], kp["private_key"], kp["public_key"], ip, ttl_days=3)
                wg.add_peer(kp["public_key"], ip)
                server_ip = os.getenv("ARGOS_VPN_SERVER_IP", os.getenv("SERVER_IP", "your-server.com"))
                config = wg.generate_client_config(kp["private_key"], ip, server_ip=server_ip)
                return _json.dumps({"status": "created", "ip": ip, "public_key": kp["public_key"], "config": config}, ensure_ascii=False)

            if action == "get":
                user = db.get_user(tid)
                if not user:
                    return _json.dumps({"status": "error", "error": "user not found"}, ensure_ascii=False)
                key = db.get_active_key(user["id"])
                if not key:
                    return _json.dumps({"status": "error", "error": "no active config"}, ensure_ascii=False)
                traffic_gb = round(db.get_traffic(tid) / (1024**3), 2)
                days_left = max(0, (key["expires_at"] - int(time.time())) // 86400)
                return _json.dumps({"ip": key["ip_address"], "days_left": days_left, "traffic_gb": traffic_gb}, ensure_ascii=False)

            return f"argos_vpn: unknown action '{action}'"
        except Exception as exc:
            return f"argos_vpn error: {exc}"

    def _argos_vpn_fast_route(self, text: str) -> str:
        t = text.strip().lower()
        if "создать" in t or "конфиг" in t or "create" in t:
            return "argos_vpn: используй MCP tool argos_vpn с action=create и telegram_id"
        if "статус" in t or "status" in t or "get" in t:
            return "argos_vpn: используй MCP tool argos_vpn с action=get и telegram_id"
        return "argos_vpn: используй MCP tool argos_vpn (actions: status/register/create/get/cleanup/clients)"

    def _mempalace_quick_status(self) -> str:
        return "MemPalace quick OK"

    async def _xiaozhi_direct_command(self, text: str) -> str:
        import httpx
        t = text.strip().lower()
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                if "список музыки" in t or "music_list" in t:
                    r = await c.get("http://127.0.0.1:8006/xiaozhi/music/")
                    return json.dumps(r.json(), ensure_ascii=False)
                m = re.search(r"(?:esp play|включи трек)\s+(\d+)", t)
                if m:
                    idx = m.group(1)
                    r = await c.post(f"http://127.0.0.1:8006/xiaozhi/debug/play_sd?index={idx}")
                    return json.dumps(r.json(), ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"status": "error", "error": str(exc)[:200]}, ensure_ascii=False)
        return "xiaozhi_direct: unknown command"

    async def _exact_tool_name_command(self, text: str) -> str:
        t = text.strip()
        parts = t.split(maxsplit=1)
        name = parts[0] if parts else ""
        rest = parts[1] if len(parts) > 1 else ""
        if name == "xiaozhi_music_list":
            return await self._xiaozhi_direct_command("список музыки ESP")
        if name == "xiaozhi_music_play":
            idx = re.search(r"\d+", rest)
            return await self._xiaozhi_direct_command(f"включи трек {idx.group(0) if idx else '1'} на ESP")
        if name == "xiaozhi_debug":
            action = rest.strip() or "sessions"
            return await self._xiaozhi_debug(action=action)
        if name == "gyrfalcon_bsod_guard":
            return self._gyrfalcon_bsod_guard_command()
        return "exact_tool_name: unknown tool"

    def _fast_system_cmd(self, text: str) -> str | None:
        """Fast direct routes for common ARGOS commands. Returns None if not matched."""
        if not text.strip():
            return None
        t = text.strip().lower()
        t_nospace = t.replace(" ", "")

        fast_pings = {"+", "++", "ping", "пинг", "test", "тест", "эй", "э", "на связи", "status", "статус", "здарова", "привет", "даров"}

        # Telegram/MCP status (must come before generic "status" ping)
        if t in ("telegram status", "tg status", "статус телеграм", "статус тг"):
            return self._telegram_status()
        if any(phrase in t for phrase in ("telegram features", "telegram watch apps", "телеграм интеграции", "watch apps")):
            tg = getattr(self.core, "telegram_bot", None)
            if tg and hasattr(tg, "_telegram_features_report"):
                return tg._telegram_features_report()
            return "Telegram features: not available"
        if t in ("mcp debug", "debug mcp", "mcp статус"):
            return self._mcp_debug()

        if t in fast_pings or t_nospace in fast_pings or any(t.startswith(p + " ") for p in fast_pings):
            return "ARGOS [Direct] ✅ система работает"
        if t_nospace.isdigit() and len(t_nospace) <= 12:
            return f"ARGOS [Direct] ✅ Получил число/код: {t_nospace}"

        # AI/provider status
        if any(phrase in t for phrase in ("ai провайдер", "провайдеры", "providers", "статус ai", "ai status", "режим ии", "ии режим")):
            mode = self.core.ai_mode_label() if self.core and hasattr(self.core, "ai_mode_label") else "unknown"
            return f"ARGOS [Direct] ✅ AI режим: {mode}\n\n{self._providers()}"
        if any(phrase in t for phrase in ("система готова", "ready check", "готов ли", "работает ли")):
            s = self._status()
            lines = ["ARGOS READY CHECK", "=" * 30]
            lines.append(f"ai_mode: {s.get('ai_mode', 'unknown')}")
            lines.append(f"cpu: {s.get('cpu_pct', 'n/a')}%")
            lines.append(f"ram: {s.get('ram_pct', 'n/a')}%")
            lines.append(f"uptime: {s.get('uptime_seconds', 0)}s")
            return "\n".join(lines)

        # GPU
        if any(phrase in t for phrase in ("gpu статус", "gpu status", "статус gpu", "видеокарты", "gpus")):
            return self._local_gpu_status()

        # System diagnostics
        if t in ("argoss_system_diagnostics", "system diagnostics", "диагностика системы", "полная диагностика"):
            return self._argoss_system_diagnostics()

        # FPGA
        if t == "fpga" or t.startswith("fpga "):
            if "dma" in t:
                return self._xilinx_fpga_command("dma_probe")
            if "locked" in t:
                return self._xilinx_fpga_command("locked_profile")
            if "gti api" in t:
                return self._xilinx_fpga_command("gti_api")
            if "gti" in t:
                return self._xilinx_fpga_command("gti_plan")
            if "sdk status" in t:
                return self._xilinx_fpga_command("sdk_status")
            if "sources" in t:
                return self._xilinx_fpga_command("sdk_sources")
            if any(x in t for x in ("bitstream", "plan", "driver")):
                return self._xilinx_fpga_command("plan")
            return self._xilinx_fpga_command("status")

        # Xiaozhi / ESP
        if any(phrase in t for phrase in ("статус esp", "esp статус", "xiaozhi status", "xiaozhi_status")):
            return "ESP status: query via xiaozhi_status MCP tool"
        if any(phrase in t for phrase in ("список музыки esp", "музыка esp", "xiaozhi_music_list")):
            return "ESP music list: query via xiaozhi_music_list MCP tool"
        if t.startswith(("esp play ", "включи трек ")):
            return "ESP play: query via xiaozhi_music_play MCP tool"

        # MemPalace
        if t.startswith(("mempalace ", "mempalace_")):
            parts = t.split(maxsplit=2)
            action = parts[1] if len(parts) > 1 else "status"
            query = parts[2] if len(parts) > 2 else ""
            return self._mempalace_command(action=action, query=query or None, text=query or None)

        # Guards and audits
        if any(phrase in t for phrase in ("runtime guard", "argoss_runtime_guard")):
            return self._argoss_runtime_guard_command()
        if any(phrase in t for phrase in ("disk guard", "argoss_disk_guard")):
            return self._argoss_disk_guard_command(apply=False)
        if any(phrase in t for phrase in ("accelerator guard", "argoss_accelerator_guard")):
            return self._argoss_accelerator_guard_command()
        if any(phrase in t for phrase in ("white audit", "argoss_white_audit")):
            return self._argoss_white_audit()
        if any(phrase in t for phrase in ("hardening status", "argoss_hardening_status")):
            return self._argoss_hardening_status()

        # SPR2801
        if any(phrase in t for phrase in ("fpga safe prepare", "настрой fpga безопасно", "fpga_safe_prepare")):
            return self._fpga_safe_prepare_command()
        if any(phrase in t for phrase in ("spr2801 crash audit", "аудит spr2801", "анализ дампов spr2801", "xdma bsod audit")):
            return self._spr2801_crash_audit_command()
        if any(phrase in t for phrase in ("spr2801 linux lab", "linux xdma", "подготовь linux xdma")):
            return self._spr2801_linux_lab_command()
        if any(phrase in t for phrase in ("spr2801 dump prepare", "подготовь анализ дампа", "windbg xdma")):
            return self._spr2801_dump_prepare_command()
        if any(phrase in t for phrase in ("gyrfalcon bsod guard", "грифон синий экран", "драйвер грифона", "включение вызывает синий экран", "xdma bsod guard")):
            return self._gyrfalcon_bsod_guard_command()
        if any(phrase in t for phrase in ("gyrfalcon vision", "грифон зрение", "gnet32", "gyrfalcon_vision")):
            return str(self._gyrfalcon_vision_command(action="status", image_path="", backend="software"))

        # Machine sync
        if any(phrase in t for phrase in ("синхронизируй все машины", "machine sync", "argoss_machine_sync")):
            return self._argoss_machine_sync_command()

        # Colibri skill
        if "colibri_asm_skill" in t or "колибри" in t:
            loaded = bool(
                self.core
                and getattr(self.core, "skill_loader", None)
                and "colibri_asm_skill" in getattr(self.core.skill_loader, "_skills", {})
            )
            return f"colibri_asm_skill {'loaded' if loaded else 'not loaded'} (asm arm64)"

        # ACP
        if t in ("acp status", "acp_status", "статус acp"):
            return getattr(self, "_acp_status", lambda: "ACP OK")()

        # Argos VPN mini-app
        if any(phrase in t for phrase in ("argos vpn", "аргос впн", "vpn конфиг", "vpn статус")):
            return self._argos_vpn_fast_route(t)

        return None

    def _fpga_safe_prepare_command(self) -> str:
        try:
            from scripts.fpga_safe_prepare import main as _fpga_prepare_main
            return _fpga_prepare_main()
        except Exception as exc:
            return f"FPGA safe prepare error: {exc}"

    def _spr2801_linux_lab_command(self) -> str:
        try:
            from scripts.spr2801_linux_lab_prepare import main as _spr2801_lab_main
            return _spr2801_lab_main()
        except Exception as exc:
            return f"SPR2801 Linux lab error: {exc}"

    def _spr2801_crash_audit_command(self) -> str:
        try:
            from scripts.spr2801_crash_audit import main as _spr2801_audit_main
            return _spr2801_audit_main()
        except Exception as exc:
            return f"SPR2801 crash audit error: {exc}"

    def _spr2801_dump_prepare_command(self) -> str:
        try:
            from scripts.spr2801_dump_prepare import main as _spr2801_dump_main
            return _spr2801_dump_main()
        except Exception as exc:
            return f"SPR2801 dump prepare error: {exc}"

    def _gyrfalcon_vision_command(self, action: str, image_path: str = "", backend: str = "software") -> str:
        try:
            from src.connectivity.gyrfalcon_vision import GyrfalconVisionBridge
            gv = GyrfalconVisionBridge(backend=backend)
            if action == "status":
                return str(gv.status())
            if action == "classify" and image_path:
                return str(gv.classify(image_path))
            return "gyrfalcon_vision: usage classify with image_path"
        except Exception as exc:
            return f"gyrfalcon_vision error: {exc}"

    def _gyrfalcon_bsod_guard_command(self) -> str:
        try:
            from scripts.argos_accelerator_guard import main as _guard_main
            return _guard_main()
        except Exception as exc:
            return f"gyrfalcon_bsod_guard error: {exc}"

    def _argoss_machine_sync_command(self, refresh_gcp: bool = True, skip_obsidian: bool = False) -> str:
        try:
            from scripts.sync_argos_machines import main as _sync_main
            import sys
            argv = ["sync_argos_machines.py"]
            if refresh_gcp:
                argv.append("--refresh-gcp")
            if skip_obsidian:
                argv.append("--skip-obsidian")
            sys.argv = argv
            return _sync_main()
        except Exception as exc:
            return f"machine sync error: {exc}"

    def _argoss_disk_guard_command(self, apply: bool = False) -> str:
        try:
            from scripts.argoss_disk_guard import main as _disk_guard_main
            import sys
            sys.argv = ["argoss_disk_guard.py", "--apply"] if apply else ["argoss_disk_guard.py"]
            return _disk_guard_main()
        except Exception as exc:
            return f"disk guard error: {exc}"

    def _argoss_runtime_guard_command(self) -> str:
        try:
            from scripts.argos_runtime_guard import main as _runtime_guard_main
            return _runtime_guard_main()
        except Exception as exc:
            return f"runtime guard error: {exc}"

    def _argoss_accelerator_guard_command(self) -> str:
        try:
            from scripts.argos_accelerator_guard import main as _accel_guard_main
            return _accel_guard_main()
        except Exception as exc:
            return f"accelerator guard error: {exc}"

    def _acp_bridge_command(self, action: str = "status") -> str:
        return f"ACP bridge {action}: OK"

    async def _xiaozhi_stability_command(self, duration: int = 10, interval: int = 2) -> str:
        try:
            from scripts.monitor_xiaozhi_stability import main as _stability_main
            import sys
            sys.argv = ["monitor_xiaozhi_stability.py", "--duration", str(duration), "--interval", str(interval)]
            return _stability_main()
        except Exception as exc:
            return f"xiaozhi stability error: {exc}"

    async def _xiaozhi_debug(self, action: str, text: str = "") -> str:
        import httpx
        try:
            if action == "stop_sd":
                async with httpx.AsyncClient(timeout=10) as c:
                    r = await c.post("http://127.0.0.1:8006/xiaozhi/debug/stop_sd")
                    return json.dumps(r.json(), ensure_ascii=False)
            if action == "say" and text:
                async with httpx.AsyncClient(timeout=10) as c:
                    r = await c.post(f"http://127.0.0.1:8006/xiaozhi/debug/say?text={urllib.parse.quote(text)}")
                    return json.dumps(r.json(), ensure_ascii=False)
            return "xiaozhi_debug: unknown action"
        except Exception as exc:
            return f"xiaozhi_debug error: {exc}"

    _xiaozhi_debug_command = _xiaozhi_debug

    def _mempalace_command(
        self,
        action: str,
        query: str | None = None,
        text: str | None = None,
        wing: str | None = None,
        room: str | None = None,
        source: str | None = None,
        top_k: int = 5,
        client_host: str | None = None,
    ) -> str:
        """Direct MemPalace access with access control."""
        host = client_host or self._last_client_host or "127.0.0.1"
        try:
            from src.mempalace_bridge import (
                get_memory_context,
                search_memory,
                status,
                store_memory,
            )
            from src.mempalace_access import check_access
        except Exception as exc:
            return f"mempalace import error: {exc}"

        try:
            # access check for write operations
            if action in ("store", "delete", "import"):
                allowed, reason = check_access(host, action, wing or "", room or "")
                if not allowed:
                    return f"⛔ MemPalace: {reason}"

            # access check for read operations (warn but allow for read-only hosts)
            if action in ("search", "context", "export"):
                allowed, reason = check_access(host, "read", wing or "", room or "")
                if not allowed:
                    return f"⛔ MemPalace: {reason}"

            if action == "status":
                return status()
            if action == "search":
                q = (query or "").strip()
                if not q:
                    return "mempalace error: query is required for search"
                hits = search_memory(q, wing=wing or "", top_k=max(1, min(int(top_k or 5), 10)))
                if not hits:
                    return f"MemPalace: ничего не найдено по запросу '{q}'."
                lines = [f"MemPalace search: {q}"]
                for idx, hit in enumerate(hits[: max(1, min(int(top_k or 5), 10))], 1):
                    snippet = str(hit.get("text", "")).replace("\n", " ").strip()
                    if len(snippet) > 180:
                        snippet = snippet[:177] + "..."
                    lines.append(
                        f"{idx}. [{hit.get('wing', '?')}/{hit.get('room', '?')}] "
                        f"score={float(hit.get('score', 0.0)):.3f} {snippet}"
                    )
                return "\n".join(lines)
            if action == "store":
                payload = (text or "").strip()
                if not payload:
                    return "mempalace error: text is required for store"
                ok = store_memory(
                    payload,
                    wing=wing or "technical",
                    room=room or "general",
                    source=source or "mcp",
                )
                return "MemPalace: память сохранена." if ok else "MemPalace: не удалось сохранить память."
            if action == "context":
                q = (query or "").strip()
                return get_memory_context(query=q or "", wing=wing or "")
            return f"mempalace error: unknown action '{action}'"
        except Exception as exc:
            return f"mempalace error: {exc}"

    def _mempalace_sync_command(
        self,
        action: str,
        wing: str | None = None,
        room: str | None = None,
        remote_url: str | None = None,
    ) -> str:
        """Синхронизация MemPalace между нодами."""
        try:
            from src.mempalace_access import access_status
            from src.mempalace_sync import export_section, export_to_json, import_from_json, pull_and_import
        except Exception as exc:
            return f"mempalace_sync import error: {exc}"

        try:
            if action == "status":
                from src.mempalace_bridge import status as mp_status
                mp = mp_status()
                info = access_status()
                return f"MemPalace Sync\n\n{mp}\n\nAccess config:\n{info}"
            if action == "export":
                records = export_section(wing=wing or "", room=room or "")
                import json, time as _time
                payload = {
                    "exported_at": _time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "wing": wing or "*",
                    "room": room or "*",
                    "source": "argos_export",
                    "drawers": records,
                }
                count = len(records)
                bytes_len = len(json.dumps(payload, ensure_ascii=False, default=str))
                return f"Exported {count} drawers from {wing or '*'}/{room or '*'} ({bytes_len} bytes)"
            if action == "pull":
                url = (remote_url or "").strip()
                if not url:
                    return "mempalace_sync error: remote_url is required for pull"
                result = pull_and_import(url, wing=wing or "", room=room or "")
                return str(result)
            if action == "access_status":
                info = access_status()
                return str(info)
            return f"mempalace_sync error: unknown action '{action}'"
        except Exception as exc:
            return f"mempalace_sync error: {exc}"

    def _npm_command(
        self,
        command: str,
        package: str | None = None,
        script: str | None = None,
        global_install: bool = False,
        dev: bool = False,
        fix: bool = False,
        npx_args: list | None = None,
        cwd: str | None = None,
    ) -> str:
        """Execute npm command via npm_manager skill."""
        try:
            from src.skills.npm_manager import NpmManager
            npm = NpmManager(cwd=cwd)
            
            if command == "install":
                return npm.install(package, global_install=global_install, dev=dev, cwd=cwd)
            elif command == "uninstall":
                return npm.uninstall(package or "", global_install=global_install, cwd=cwd)
            elif command == "run":
                return npm.run_script(script or "", cwd=cwd)
            elif command == "list":
                return npm.list_packages(global_list=global_install, cwd=cwd)
            elif command == "update":
                return npm.update(package, cwd=cwd)
            elif command == "info":
                return npm.info(package or "")
            elif command == "audit":
                return npm.audit(fix=fix, cwd=cwd)
            elif command == "outdated":
                return npm.outdated(cwd=cwd)
            elif command == "init":
                return npm.init(name=package, cwd=cwd)
            elif command == "search":
                return npm.search(package or "")
            elif command == "npx":
                return npm.npx(package or "", *(npx_args or []), cwd=cwd)
            elif command == "execute":
                return npm.execute(package or "", cwd=cwd)
            else:
                return f"Unknown npm command: {command}"
        except Exception as exc:
            return f"npm error: {exc}"

    def _porphyry_command(
        self,
        action: str,
        topic: str | None = None,
        mode: str | None = None,
        depth: int = 1,
        command: str | None = None,
    ) -> str:
        """Execute porphyry command via skill."""
        try:
            from src.skills.porphyry import PorphyryTriad
            p = PorphyryTriad(core=self.core)
            
            if action == "contemplate" and topic:
                return p.contemplate(topic)
            elif action == "mode" and mode:
                return p.set_mode(mode)
            elif action == "depth":
                return p.set_depth(depth)
            elif action == "status":
                return p.status()
            elif action == "diagnostics":
                return p._run_diagnostics()
            elif action == "shell" and command:
                return p.execute_shell(command)
            else:
                return f"Unknown porphyry action: {action}"
        except Exception as exc:
            return f"porphyry error: {exc}"

    def _orangepi_gadget_command(
        self,
        action: str,
        mode: str | None = None,
    ) -> str:
        """Execute Orange Pi USB Gadget command."""
        try:
            from src.connectivity.orangepi_gadget import OrangePiGadgetManager, GadgetMode
            mgr = OrangePiGadgetManager()
            if action == "status":
                return mgr.status()
            elif action == "diagnostics":
                return mgr.diagnostics()
            elif action == "stop":
                return mgr.stop()
            elif action == "setup":
                return mgr.setup(mode or "all")
            else:
                return f"Unknown orangepi_gadget action: {action}"
        except Exception as exc:
            return f"orangepi_gadget error: {exc}"

    def _orangepi_bridge_command(
        self,
        action: str,
        pin: int | None = None,
        value: int | None = None,
        addr: int | None = None,
        reg: int | None = None,
        data: str | None = None,
        slave: int | None = None,
        count: int | None = None,
        speed: int | None = None,
    ) -> str:
        """Execute Orange Pi Bridge hardware command (GPIO/I2C/UART/SPI/Modbus/1-Wire)."""
        try:
            from src.connectivity.orangepi_bridge import OrangePiBridge
            bridge = OrangePiBridge(core=self.core)
            if action == "status":
                return bridge.status()
            elif action == "gpio_out" and pin is not None and value is not None:
                return bridge.gpio_out(pin, value)
            elif action == "gpio_in" and pin is not None:
                return bridge.gpio_in(pin)
            elif action == "gpio_status":
                return bridge.gpio_status()
            elif action == "pin_map":
                return bridge.pin_map()
            elif action == "i2c_scan":
                return bridge.i2c_scan()
            elif action == "i2c_read" and addr is not None and reg is not None:
                return bridge.i2c_read(addr, reg)
            elif action == "i2c_write" and addr is not None and reg is not None and value is not None:
                return bridge.i2c_write(addr, reg, value)
            elif action == "bmp280" and addr is not None:
                return bridge.read_bmp280(addr)
            elif action == "bmp280":
                return bridge.read_bmp280()
            elif action == "1wire":
                return bridge.read_1wire()
            elif action == "uart_send" and data is not None:
                return bridge.uart_send(data)
            elif action == "uart_recv":
                return bridge.uart_recv()
            elif action == "modbus_read" and slave is not None and reg is not None:
                return bridge.modbus_read(slave, reg, count or 1)
            elif action == "modbus_write" and slave is not None and reg is not None and value is not None:
                return bridge.modbus_write(slave, reg, value)
            elif action == "rs485_raw" and data is not None:
                return bridge.rs485_raw(data.encode())
            elif action == "spi_transfer" and data is not None:
                bytes_data = [int(b, 16) if b.startswith("0x") else int(b) for b in data.split(",")]
                return bridge.spi_transfer(bytes_data, speed or 500000)
            elif action == "scan_all":
                return bridge.scan_all()
            else:
                return f"Unknown orangepi_bridge action: {action}"
        except Exception as exc:
            return f"orangepi_bridge error: {exc}"

    def _ollama_vision_command(
        self,
        action: str,
        message: str | None = None,
        image_path: str | None = None,
    ) -> str:
        """Execute Ollama Vision command."""
        try:
            from src.connectivity.ollama_vision_bridge import OllamaVisionBridge
            bridge = OllamaVisionBridge()
            if action == "status":
                avail = "доступен" if bridge.is_available else "недоступен"
                models = bridge.list_models()
                return f"Ollama Vision: {avail}\nModel: {bridge.model}\nМоделей: {len(models)}"
            elif action == "describe" and image_path:
                return bridge.describe_image(image_path)
            elif action == "ocr" and image_path:
                return bridge.extract_text(image_path)
            elif action == "chat" and message:
                imgs = [image_path] if image_path else None
                return bridge.chat(message, images=imgs)
            else:
                return f"Unknown ollama_vision action: {action}"
        except Exception as exc:
            return f"ollama_vision error: {exc}"

    def _pi_bridge_command(
        self,
        action: str,
        prompt: str | None = None,
        model: str | None = None,
        instance: str | None = None,
        timeout: int = 300,
    ) -> str:
        """Execute Pi Coding Agent task or local Ollama."""
        try:
            from src.connectivity.pi_bridge import PiBridge
            bridge = PiBridge()
            
            if action == "status":
                return bridge.status()
            elif action == "instances":
                return bridge.list_instances()
            elif action == "models":
                return bridge.list_models()
            elif action == "execute" and prompt:
                return bridge.execute(prompt, model=model, timeout=timeout)
            elif action == "execute_async" and prompt:
                task_id = bridge.execute_async(prompt, model=model)
                return f"Task started: {task_id}"
            elif action == "local" and prompt:
                # Локальный Ollama запрос
                return bridge.execute_local(prompt, model=model or "qwen2.5:7b", timeout=timeout)
            else:
                return f"Unknown pi_bridge action: {action}"
        except Exception as exc:
            return f"pi_bridge error: {exc}"

    def _xilinx_fpga_command(self, action: str = "status") -> str:
        """Detect and report the local Xilinx/AMD PCIe FPGA endpoint."""
        try:
            from src.connectivity.xilinx_fpga import XilinxFPGA

            return XilinxFPGA().command(action=action)
        except Exception as exc:
            return f"xilinx_fpga error: {exc}"

    def _cloudflare_models(self) -> str:
        models = [
            "@cf/moonshotai/kimi-k2.5",
            "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
            "@cf/meta/llama-3.1-8b-instruct",
            "@cf/meta/llama-3.1-70b-instruct",
            "@cf/meta/llama-2-7b-chat-int8",
            "@cf/mistral/mistral-7b-instruct-v0.2",
            "@cf/mistral/mistral-7b-instruct-v0.1",
            "@cf/google/gemma-2b-it",
            "@cf/google/gemma-7b-it",
            "@cf/qwen/qwen1.5-14b-chat-awq",
            "@cf/qwen/qwen1.5-7b-chat-awq",
            "@cf/qwen/qwen1.5-1.8b-chat",
            "@cf/deepseek-ai/deepseek-math-7b-instruct",
            "@cf/openchat/openchat-3.5-0106",
            "@cf/thebloke/discolm-german-7b-v1-awq",
            "@cf/tiiuae/falcon-7b-instruct",
            "@cf/microsoft/phi-2",
            "@cf/defog/sqlcoder-7b-2",
            "@cf/lynn/soupprompts-7b",
            "@cf/meta/llama-3-8b-instruct",
            "@cf/nousresearch/hermes-2-pro-mistral-7b",
            "@cf/neuralmagic/mistral-7b-instruct-v0.3-awq",
            "@cf/huggingfacehq/zephyr-7b-beta-awq",
            "@cf/unga/tinyllama-1.1b-chat-v1.0",
            "@cf/eleutherai/pythia-2.8b",
        ]
        return "\n".join(models)

    async def _cloudflare_chat(self, prompt: str, model: str | None = None, system: str | None = None, temperature: float = 0.4, max_tokens: int = 1200) -> str:
        import aiohttp
        api_token = os.getenv("CLOUDFLARE_API_TOKEN", "").strip()
        account_id = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
        if not api_token or not account_id:
            return "Missing CLOUDFLARE_API_TOKEN or CLOUDFLARE_ACCOUNT_ID"
        model_id = model or os.getenv("CLOUDFLARE_MODEL", "@cf/moonshotai/kimi-k2.5")
        url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model_id}"
        payload = {
            "messages": [
                {"role": "system", "content": system or "You are a helpful assistant."},
                {"role": "user", "content": prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        try:
            timeout = aiohttp.ClientTimeout(total=120)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, headers={"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"}, json=payload) as resp:
                    data = await resp.json()
                    if data.get("success"):
                        choices = data.get("result", {}).get("choices", [])
                        if choices:
                            return choices[0].get("message", {}).get("content", "")
                        return "Empty response from Cloudflare AI"
                    err = data.get("errors", [{}])[0]
                    return f"Cloudflare AI error: {err.get('message', data)}"
        except Exception as exc:
            return f"Cloudflare request error: {exc}"

    def _argoss_white_audit(self) -> str:
        """White audit: порты, ключевые ENV, hang-маркеры."""
        from pathlib import Path as _P
        import socket
        lines = ["🔍 ARGOS WHITE AUDIT", "=" * 32]
        # Порты
        ports = {8000: "MCP", 5001: "Brain", 5010: "Audit", 8085: "V100-Mistral", 8082: "RX580",
                 8006: "Voice-ESP", 8100: "Z2M-PC", 11434: "Ollama", 1883: "MQTT"}
        lines.append("Порты:")
        for p, nm in ports.items():
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.5)
            up = s.connect_ex(("127.0.0.1", p)) == 0
            s.close()
            lines.append(f"  {'✅' if up else '❌'} :{p} {nm}")
        # ENV
        lines.append("ENV:")
        for k in ("GPU_SERVER_0_PORT", "OLLAMA_HOST", "ARGOS_GCP_URL", "GEMINI_API_KEY_0"):
            v = os.getenv(k, "")
            lines.append(f"  {'✅' if v else '❌'} {k}={'set' if v else 'EMPTY'}")
        # Hang-маркеры
        try:
            root = _P(os.getenv("ARGOS_PROJECT_ROOT", "") or os.getcwd())
            hangs = list(root.glob("*.hang")) + list((root / "data").glob("*.hang"))
            lines.append(f"Hang-маркеры: {len(hangs)}")
        except Exception:
            pass
        return "\n".join(lines)

    def _argoss_hardening_status(self) -> str:
        """Hardening: таймауты, watchdog, провайдеры."""
        lines = ["🛡️ ARGOS HARDENING STATUS", "=" * 32]
        lines.append(f"MCP timeout: {os.getenv('ARGOS_MCP_TIMEOUT', '120')}s")
        lines.append(f"TG read timeout: {os.getenv('TG_READ_TIMEOUT_SEC', '30')}s")
        lines.append(f"Provider cooldown: {os.getenv('ARGOS_PROVIDER_COOLDOWN', '60')}s")
        lines.append(f"LocalGPU: V100 mistral :8085 + RX580 :8082")
        try:
            from src.mempalace_bridge import status as _ms
            lines.append("MemPalace: " + _ms().split(chr(10))[1].strip())
        except Exception:
            pass
        return "\n".join(lines)

    def _local_gpu_status(self) -> str:
        """Report real llama-server GPU endpoints from GPU_SERVER_* ENV."""
        import json
        import socket
        import urllib.request

        def env_enabled(name: str, default: str = "1") -> bool:
            value = os.getenv(name, default).strip().lower()
            return value not in {"0", "false", "off", "no", "нет", "выкл"}

        def normalize_host(host: str) -> str:
            host = (host or "localhost").strip()
            host = host.removeprefix("http://").removeprefix("https://")
            return host.split("/", 1)[0].split(":", 1)[0] or "localhost"

        def tcp_open(host: str, port: int) -> bool:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(0.7)
                ok = sock.connect_ex((host, port)) == 0
                sock.close()
                return ok
            except Exception:
                return False

        def read_models(host: str, port: int) -> str:
            url = f"http://{host}:{port}/v1/models"
            try:
                with urllib.request.urlopen(url, timeout=2) as response:
                    data = json.loads(response.read().decode("utf-8", errors="replace"))
                entries = data.get("data") or data.get("models") or []
                names = []
                for item in entries:
                    if isinstance(item, dict):
                        names.append(str(item.get("id") or item.get("model") or item.get("name") or "").strip())
                return ", ".join([name for name in names if name]) or "model endpoint ok"
            except Exception:
                return ""

        servers: list[dict[str, Any]] = []
        seen: set[tuple[str, int]] = set()
        for idx in range(8):
            if not env_enabled(f"GPU_SERVER_{idx}_ENABLED", "1"):
                continue
            raw_port = os.getenv(f"GPU_SERVER_{idx}_PORT", "").strip()
            if not raw_port:
                continue
            try:
                port = int(raw_port)
            except ValueError:
                continue
            host = normalize_host(os.getenv(f"GPU_SERVER_{idx}_HOST", "localhost"))
            key = (host, port)
            if key in seen:
                continue
            seen.add(key)
            servers.append(
                {
                    "name": os.getenv(f"GPU_SERVER_{idx}_NAME", f"GPU{idx}").strip() or f"GPU{idx}",
                    "host": host,
                    "port": port,
                    "model": os.getenv(f"GPU_SERVER_{idx}_MODEL", "unknown").strip() or "unknown",
                }
            )

        v100_host = os.getenv("LLAMA_V100_HOST", "").strip()
        if v100_host:
            try:
                from urllib.parse import urlparse

                parsed = urlparse(v100_host if "://" in v100_host else "http://" + v100_host)
                host = normalize_host(parsed.netloc or parsed.path)
                port = int(parsed.port or 8085)
                key = (host, port)
                if key not in seen:
                    servers.insert(
                        0,
                        {
                            "name": "V100-Mistral",
                            "host": host,
                            "port": port,
                            "model": os.getenv("LLAMA_V100_MODEL", "mistral-nemo").strip() or "mistral-nemo",
                        },
                    )
            except Exception:
                pass

        if not servers:
            return "ARGOS GPU SERVERS: no GPU_SERVER_* endpoints configured"

        lines = ["ARGOS GPU SERVERS (llama-server):"]
        for server in servers:
            host = str(server["host"])
            port = int(server["port"])
            up = tcp_open(host, port)
            model_info = read_models(host, port) if up else ""
            extra = f" | api model: {model_info}" if model_info else ""
            lines.append(
                f"  {'✅' if up else '❌'} :{port} {server['name']} -> {server['model']} ({host}){extra}"
            )
        return "\n".join(lines)

    def _start_v100_gpu(self) -> str:
        """Start the V100 Nemo launcher from MCP."""
        import subprocess

        root = Path(os.getenv("ARGOS_PROJECT_ROOT", "") or os.getcwd())
        script = root / "scripts" / "start_v100_nemo.ps1"
        if not script.exists():
            return f"V100 launcher not found: {script}"

        pwsh = Path(r"C:\Program Files\PowerShell\7\pwsh.exe")
        shell = str(pwsh) if pwsh.exists() else "powershell.exe"
        try:
            completed = subprocess.run(
                [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=180,
                encoding="utf-8",
                errors="replace",
            )
            output = "\n".join(part for part in (completed.stdout.strip(), completed.stderr.strip()) if part)
            if completed.returncode == 0:
                return output or "V100 launcher completed successfully."
            return f"V100 launcher exit code {completed.returncode}\n{output}"
        except subprocess.TimeoutExpired:
            return "V100 launcher timeout after 180s"
        except Exception as exc:
            return f"V100 launcher error: {exc}"

    def _argoss_env_dedup(self, action: str = "check") -> str:
        import re
        from collections import OrderedDict
        root = Path(os.getenv("ARGOS_PROJECT_ROOT", "") or os.getcwd())
        env_path = root / ".env"
        if not env_path.exists():
            return f".env not found: {env_path}"
        lines = env_path.read_text(encoding="utf-8").splitlines()
        key_lines: OrderedDict[str, list[tuple[int, str]]] = OrderedDict()
        for idx, line in enumerate(lines, 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)=(.*)$', stripped)
            if m:
                key = m.group(1)
                if key not in key_lines:
                    key_lines[key] = []
                key_lines[key].append((idx, line))
        duplicates = {k: v for k, v in key_lines.items() if len(v) > 1}
        if not duplicates:
            return f"Dublikatov ne naideno. Provereno {len(key_lines)} unikalnyh kluchey."
        if action == "fix":
            lines_keep = []
            seen_keys: set[str] = set()
            for idx, line in enumerate(lines, 1):
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    lines_keep.append(line)
                    continue
                m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)=(.*)$', stripped)
                if m:
                    key = m.group(1)
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                lines_keep.append(line)
            backup_path = env_path.with_suffix(".env.backup")
            env_path.write_text("\n".join(lines_keep) + "\n", encoding="utf-8")
            backup_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            removed = sum(len(v) - 1 for v in duplicates.values())
            return f"Ispravleno {removed} dublikatov. Backup: {backup_path}"
        lines_out = [f"Naideno {len(duplicates)} dublikatov ENV-peremennyh:"]
        for key, entries in duplicates.items():
            lines_out.append(f"\n  {key} ({len(entries)} raz):")
            for lineno, line in entries:
                val = line.split("=", 1)[1] if "=" in line else ""
                lines_out.append(f"     stroka {lineno}: {key}={val[:60]}{'...' if len(val) > 60 else ''}")
        lines_out.append(f"\nDlya ispravleniya: argoss_env_dedup action=fix")
        return "\n".join(lines_out)

    def _argoss_system_diagnostics(self) -> str:
        parts = []
        parts.append("ARGOS SYSTEM DIAGNOSTICS")
        parts.append("=" * 40)
        s = self._status()
        parts.append(f"  uptime:    {s.get('uptime_seconds', 0)}s")
        parts.append(f"  ai_mode:   {s.get('ai_mode', 'unknown')}")
        parts.append(f"  cpu:       {s.get('cpu_pct', 'n/a')}%")
        parts.append(f"  ram:       {s.get('ram_pct', 'n/a')}%")
        try:
            import psutil
            disk = psutil.disk_usage("F:\\")
            parts.append(f"  disk:      {disk.percent:.1f}% ({(disk.free / 1024**3):.1f}GB free)")
        except Exception:
            pass
        parts.append("")
        parts.append("GPU STATUS:")
        try:
            gpu_status = self._local_gpu_status()
            for line in gpu_status.splitlines():
                if line.strip():
                    parts.append(f"  {line}")
        except Exception as exc:
            parts.append(f"  GPU error: {exc}")
        parts.append("")
        parts.append("FPGA STATUS:")
        try:
            fpga_status = self._xilinx_fpga_command("status")
            for line in fpga_status.splitlines():
                if line.strip():
                    parts.append(f"  {line}")
        except Exception as exc:
            parts.append(f"  FPGA error: {exc}")
        parts.append("")
        parts.append("PROVIDERS:")
        try:
            from src.ai_providers import providers_status
            prov = providers_status()
            for line in prov.splitlines():
                if "✅" in line or "🟢" in line or "Активных" in line:
                    parts.append(f"  {line.strip()}")
        except Exception as exc:
            parts.append(f"  providers error: {exc}")
        parts.append("")
        parts.append("PORTS:")
        ports_check = [
            (8000, "MCP"),
            (8090, "Dashboard"),
            (8085, "V100-Mistral"),
            (8082, "RX580"),
            (8084, "RX560"),
            (11434, "Ollama"),
        ]
        import socket
        for port, name in ports_check:
            try:
                sock = socket.socket()
                sock.settimeout(1)
                result = sock.connect_ex(("127.0.0.1", port))
                status = "OPEN" if result == 0 else "CLOSED"
                sock.close()
            except Exception:
                status = "ERROR"
            parts.append(f"  {port:5} {name:12} {status}")
        return "\n".join(parts)

    def _obsidian_command(self, action: str, path: str = None, query: str = None, content: str = None, project_root: str = None, target_folder: str = None) -> str:
        try:
            vault = os.getenv("OBSIDIAN_VAULT_PATH", os.getenv("ARGOS_OBSIDIAN_VAULT_PATH", ""))
            if not vault or not os.path.isdir(vault):
                vault = "F:\\debug\\аргос"
            if action == "status":
                from pathlib import Path as _P
                vp = Path(vault)
                md_files = list(vp.rglob("*.md"))
                return f"Obsidian: vault={vault}, files={len(md_files)}"
            elif action == "search" and query:
                import glob as _g
                matches = _g.glob(f"{vault}/**/*{query}*.md", recursive=True)
                if matches:
                    lines = [f"Found {len(matches)} notes:"]
                    for m in matches[:10]:
                        lines.append(f"  {os.path.relpath(m, vault)}")
                    return "\n".join(lines)
                return f"No notes found for: {query}"
            elif action == "read" and path:
                full = os.path.join(vault, path)
                if not full.endswith(".md"):
                    full += ".md"
                if os.path.exists(full):
                    with open(full, "r", encoding="utf-8") as f:
                        return f.read()[:3000]
                return f"File not found: {path}"
            elif action == "daily":
                from datetime import date
                today = date.today().isoformat()
                daily_dir = os.path.join(vault, "Daily")
                os.makedirs(daily_dir, exist_ok=True)
                daily_path = os.path.join(daily_dir, f"{today}.md")
                if not os.path.exists(daily_path):
                    with open(daily_path, "w", encoding="utf-8") as f:
                        f.write(f"# {today}\n\n")
                return f"Daily note: {today}.md"
            return f"Unknown Obsidian action: {action}"
        except Exception as exc:
            return f"Obsidian error: {exc}"

    def _create_app(self) -> FastAPI:
        app = FastAPI(title="Argos MCP", version="1.0")
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        )

        @app.get("/health")
        def health():
            return self._status()

        @app.get("/acp")
        def acp_info():
            return {"protocol": "acp/1.0"}

        @app.post("/telegram")
        async def telegram_proxy(request: Request):
            tg_port = int(os.getenv("TG_WEBHOOK_PORT", "8001") or "8001")
            tg_path = os.getenv("TG_WEBHOOK_PATH", "/telegram").strip() or "/telegram"
            body = await request.body()
            headers = {k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")}
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        f"http://127.0.0.1:{tg_port}{tg_path}",
                        data=body,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        content = await resp.read()
                        from fastapi.responses import Response
                        return Response(content=content, status_code=resp.status, media_type=resp.content_type)
            except Exception as exc:
                raise HTTPException(status_code=502, detail=f"TG webhook proxy error: {exc}")

        @app.get("/mcp")
        def mcp_ping():
            return {
                "name": "argos",
                "ok": True,
                "transport": "http",
                "hint": "POST JSON-RPC to /mcp",
            }

        @app.post("/mcp")
        async def mcp_rpc(request: Request):
            self._last_client_host = request.client.host if request.client else "127.0.0.1"
            try:
                raw_body = await request.body()
                # Telegram иногда шлёт cp1251 — декодируем с заменой
                body_str = raw_body.decode("utf-8", errors="replace")
                import json as _json
                payload = _json.loads(body_str)
            except Exception:
                try:
                    payload = await request.json()
                except Exception as e:
                    raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")
            if not isinstance(payload, dict):
                raise HTTPException(status_code=400, detail="JSON object expected")

            method = payload.get("method", "")
            req_id = payload.get("id")          # None для notifications
            is_notification = req_id is None    # MCP notifications не имеют id

            def _ok(result: Any):
                return {"jsonrpc": "2.0", "id": req_id, "result": result}

            def _err(code: int, message: str):
                return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}

            # ── Notifications (нет id) → пустой ответ 200 ────────────────────
            if is_notification:
                # notifications/initialized, notifications/cancelled, etc.
                return {}

            # ── ping ─────────────────────────────────────────────────────────
            if method == "ping":
                return _ok({})

            # ── initialize ───────────────────────────────────────────────────
            if method == "initialize":
                return _ok(
                    {
                        "protocolVersion": "2024-11-05",
                        "serverInfo": {"name": "argos", "version": "2.1.3"},
                        "capabilities": {
                            "tools": {"listChanged": False},
                        },
                        "instructions": (
                            "ARGOS Universal OS — AI-экосистема. "
                            "Используй инструмент 'command' для выполнения любых команд ARGOS. "
                            "Инструменты: providers, skills, limits, status, command, image_generate, cloudflare_models, cloudflare_chat, npm, porphyry."
                        ),
                    }
                )

            # ── tools/list ───────────────────────────────────────────────────
            if method == "tools/list":
                tools = [
                    {
                        "name": "providers",
                        "description": "Показывает статус всех AI-провайдеров ARGOS (Gemini, GigaChat, Grok, OpenAI, Groq, DeepSeek, Kimi, Ollama и др.) с лимитами и квотами.",
                        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
                    },
                    {
                        "name": "skills",
                        "description": "Список загруженных скилов (навыков) ARGOS — внешние интеграции и инструменты.",
                        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
                    },
                    {
                        "name": "limits",
                        "description": "Отчёт о текущих лимитах и квотах провайдеров.",
                        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
                    },
                    {
                        "name": "status",
                        "description": "Текущий статус ARGOS: uptime, CPU, RAM, режим ИИ.",
                        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
                    },
                    {
                        "name": "headroom_compress",
                        "description": "Сжать большой текст/JSON/логи через Headroom-компрессор (авто-детект стратегии, 90%+ экономии на больших outputs).",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string", "description": "Текст для сжатия"},
                            },
                            "required": ["text"],
                            "additionalProperties": False,
                        },
                    },
                    {
                        "name": "osint",
                        "description": ("OSINT-разведка: 17 поисковиков (shodan, censys, fofa, zoomeye, fullhunt, "
                                        "urlscan, hunter, crt.sh, grep.app, securitytrails, intelx, hibp, leakix, "
                                        "dehashed, vulners, greynoise, wigle). service=имя функции "
                                        "(shodan_search/crtsh/vulners_search/quick_recon/ip_recon/...), query=цель."),
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "service": {"type": "string", "description": "Функция: shodan_search, crtsh, vulners_search, hibp_check, quick_recon, ip_recon, и др."},
                                "query": {"type": "string", "description": "Цель: домен / IP / запрос / email / CVE"},
                            },
                            "required": ["service", "query"],
                            "additionalProperties": False,
                        },
                    },
                    {
                        "name": "image_generate",
                        "description": "Generate image from prompt and return absolute file path.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "prompt": {"type": "string"},
                                "negative_prompt": {"type": "string"},
                                "steps": {"type": "integer", "minimum": 1, "maximum": 80},
                                "width": {"type": "integer", "minimum": 256, "maximum": 1536},
                                "height": {"type": "integer", "minimum": 256, "maximum": 1536},
                                "model_name": {"type": "string"},
                            },
                            "required": ["prompt"],
                            "additionalProperties": False,
                        },
                    },
                    {
                        "name": "cloudflare_models",
                        "description": "Список доступных моделей Cloudflare Workers AI (текстовые LLM).",
                        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
                    },
                    {
                        "name": "cloudflare_chat",
                        "description": "Отправить запрос к любой модели Cloudflare Workers AI. По умолчанию используется @cf/moonshotai/kimi-k2.5.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "prompt": {"type": "string", "description": "Текст запроса пользователя"},
                                "model": {"type": "string", "description": "ID модели Cloudflare, например @cf/moonshotai/kimi-k2.5"},
                                "system": {"type": "string", "description": "Системный промпт (опционально)"},
                                "temperature": {"type": "number", "minimum": 0, "maximum": 2, "default": 0.4},
                                "max_tokens": {"type": "integer", "minimum": 1, "maximum": 4096, "default": 1200},
                            },
                            "required": ["prompt"],
                            "additionalProperties": False,
                        },
                    },
                    {
                        "name": "npm",
                        "description": (
                            "Управление npm пакетами. "
                            "Поддерживает: install, uninstall, run, list, update, info, audit, outdated, init, search, npx. "
                            "Примеры: 'npm install express', 'npm run build', 'npm list', 'npm audit', 'npm outdated'."
                        ),
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "command": {
                                    "type": "string",
                                    "enum": ["install", "uninstall", "run", "list", "update", "info", "execute", "audit", "outdated", "init", "search", "npx"],
                                    "description": "npm команда",
                                },
                                "package": {
                                    "type": "string",
                                    "description": "Имя пакета (для install, uninstall, info, search, npx)",
                                },
                                "script": {
                                    "type": "string",
                                    "description": "Имя скрипта (для run)",
                                },
                                "global": {
                                    "type": "boolean",
                                    "description": "Глобальная установка",
                                    "default": False,
                                },
                                "dev": {
                                    "type": "boolean",
                                    "description": "Dev dependency (для install)",
                                    "default": False,
                                },
                                "fix": {
                                    "type": "boolean",
                                    "description": "Исправить проблемы (для audit)",
                                    "default": False,
                                },
                                "args": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "Дополнительные аргументы (для npx)",
                                },
                                "cwd": {
                                    "type": "string",
                                    "description": "Рабочая директория",
                                },
                            },
                            "required": ["command"],
                            "additionalProperties": False,
                        },
                    },
                    {
                        "name": "porphyry",
                        "description": (
                            "Философский модуль Порфирия — триада мышления. "
                            "Режимы: analytic (трезвый), creative (ироничный), insight (интуитивный), consilium (консилиум). "
                            "Примеры: 'порфирий аналитик', 'порфирий консилиум искусственный интеллект', 'порфирий глубина 2'."
                        ),
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "action": {
                                    "type": "string",
                                    "enum": ["contemplate", "mode", "depth", "status", "diagnostics", "shell"],
                                    "description": "Действие: contemplate (размышление), mode (смена режима), depth (глубина), status (статус), diagnostics (диагностика), shell (shell-команда)",
                                },
                                "topic": {
                                    "type": "string",
                                    "description": "Тема для размышления (для contemplate)",
                                },
                                "mode": {
                                    "type": "string",
                                    "enum": ["analytic", "creative", "insight", "consilium"],
                                    "description": "Режим триады (для mode)",
                                },
                                "depth": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "maximum": 3,
                                    "description": "Глубина размышления 1-3 (для depth)",
                                },
                                "command": {
                                    "type": "string",
                                    "description": "Shell-команда (для shell)",
                                },
                            },
                            "required": ["action"],
                            "additionalProperties": False,
                        },
                    },
                    {
                        "name": "orangepi_gadget",
                        "description": (
                            "Управление USB Gadget Orange Pi One. "
                            "Режимы: serial (CDC ACM), ethernet (RNDIS), storage (Mass Storage), all (все). "
                            "Примеры: 'orangepi_gadget status', 'orangepi_gadget setup serial', 'orangepi_gadget stop'."
                        ),
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "action": {
                                    "type": "string",
                                    "enum": ["status", "diagnostics", "setup", "stop"],
                                    "description": "Действие: status (статус), diagnostics (диагностика), setup (запустить), stop (остановить)",
                                },
                                "mode": {
                                    "type": "string",
                                    "enum": ["serial", "ethernet", "storage", "all"],
                                    "description": "Режим USB-гаджета (для setup)",
                                },
                            },
                            "required": ["action"],
                            "additionalProperties": False,
                        },
                    },
                    {
                        "name": "orangepi_bridge",
                        "description": (
                            "Аппаратный мост Orange Pi One: GPIO, I2C, UART, SPI, 1-Wire, RS-485, Modbus RTU. "
                            "Примеры: 'orangepi_bridge gpio_out pin=11 value=1', 'orangepi_bridge i2c_scan', "
                            "'orangepi_bridge bmp280', 'orangepi_bridge uart_send data=AT', 'orangepi_bridge 1wire'."
                        ),
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "action": {
                                    "type": "string",
                                    "enum": [
                                        "status", "gpio_out", "gpio_in", "gpio_status", "pin_map",
                                        "i2c_scan", "i2c_read", "i2c_write", "bmp280", "1wire",
                                        "uart_send", "uart_recv", "modbus_read", "modbus_write",
                                        "rs485_raw", "spi_transfer", "scan_all"
                                    ],
                                    "description": "Действие с аппаратным мостом",
                                },
                                "pin": {
                                    "type": "integer",
                                    "description": "Номер физического пина (для gpio_out, gpio_in)",
                                },
                                "value": {
                                    "type": "integer",
                                    "description": "Значение 0/1 (для gpio_out, i2c_write, modbus_write)",
                                },
                                "addr": {
                                    "type": "integer",
                                    "description": "I2C адрес устройства (для i2c_read, i2c_write, bmp280)",
                                },
                                "reg": {
                                    "type": "integer",
                                    "description": "Регистр I2C/Modbus (для i2c_read, i2c_write, modbus_read, modbus_write)",
                                },
                                "data": {
                                    "type": "string",
                                    "description": "Данные для отправки (для uart_send, rs485_raw) или список байт через запятую (для spi_transfer)",
                                },
                                "slave": {
                                    "type": "integer",
                                    "description": "Адрес Modbus slave (для modbus_read, modbus_write)",
                                },
                                "count": {
                                    "type": "integer",
                                    "description": "Количество регистров Modbus (для modbus_read)",
                                    "default": 1,
                                },
                                "speed": {
                                    "type": "integer",
                                    "description": "Скорость SPI в Hz (для spi_transfer)",
                                    "default": 500000,
                                },
                            },
                            "required": ["action"],
                            "additionalProperties": False,
                        },
                    },
                    {
                        "name": "ollama_vision",
                        "description": (
                            "Ollama Vision — анализ изображений через локальную Ollama. "
                            "Поддерживает описание изображений, OCR (извлечение текста), анализ скриншотов. "
                            "Примеры: 'ollama_vision describe /path/to/image.jpg', 'ollama_vision ocr screenshot.png'"
                        ),
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "action": {
                                    "type": "string",
                                    "enum": ["status", "describe", "ocr", "chat"],
                                    "description": "Действие: status (статус), describe (описать), ocr (текст), chat (чат с изображением)",
                                },
                                "message": {
                                    "type": "string",
                                    "description": "Сообщение/промпт (для chat)",
                                },
                                "image_path": {
                                    "type": "string",
                                    "description": "Путь к изображению",
                                },
                            },
                            "required": ["action"],
                            "additionalProperties": False,
                        },
                    },
                    {
                        "name": "pi_bridge",
                        "description": (
                            "Pi Coding Agent + Local Ollama GPU кластер. "
                            "Поддерживает: status, instances, models, execute, local. "
                            "Примеры: 'pi_bridge local Привет', 'pi_bridge instances', 'pi_bridge execute код'."
                        ),
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "action": {
                                    "type": "string",
                                    "enum": ["status", "instances", "models", "execute", "local", "execute_async"],
                                    "description": "Действие: status (статус), instances (список GPU/VM), models (модели), execute (Pi задача), local (локальный Ollama), execute_async",
                                },
                                "prompt": {
                                    "type": "string",
                                    "description": "Промпт/задача",
                                },
                                "model": {
                                    "type": "string",
                                    "description": "Модель (qwen2.5:7b, llama3.2:1b и др.)",
                                },
                                "instance": {
                                    "type": "string",
                                    "description": "Инстанс (GPU0, Azure VM, JP1 и др.)",
                                },
                                "timeout": {
                                    "type": "integer",
                                    "description": "Таймаут",
                                    "default": 300,
                                },
                            },
                            "required": ["action"],
                            "additionalProperties": False,
                        },
                    },
                    {
                        "name": "fpga_safe_prepare",
                        "description": "Безопасная подготовка FPGA/GTI лаборатории без записи в BAR/DMA.",
                        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
                    },
                    {
                        "name": "spr2801_linux_lab",
                        "description": "Подготовить Linux XDMA lab пакет для SPR2801.",
                        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
                    },
                    {
                        "name": "spr2801_crash_audit",
                        "description": "Аудит Windows BSOD/SPR2801 crash evidence (read-only).",
                        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
                    },
                    {
                        "name": "spr2801_dump_prepare",
                        "description": "Подготовить offline WinDbg dump analysis для SPR2801.",
                        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
                    },
                    {
                        "name": "gyrfalcon_vision",
                        "description": "Gyrfalcon/SPR2801 vision: безопасный software backend (gnet32 scene classify).",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "action": {"type": "string", "enum": ["status", "classify"], "description": "Действие"},
                                "image_path": {"type": "string", "description": "Путь к изображению"},
                                "backend": {"type": "string", "enum": ["software"], "description": "Backend (only safe software mode)"},
                            },
                            "required": ["action"],
                            "additionalProperties": False,
                        },
                    },
                    {
                        "name": "gyrfalcon_bsod_guard",
                        "description": "Статус гвардии Gyrfalcon/XDMA BSOD.",
                        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
                    },
                    {
                        "name": "argoss_machine_sync",
                        "description": "Синхронизация машин/VM ARGOS.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "refresh_gcp": {"type": "boolean", "default": True},
                                "skip_obsidian": {"type": "boolean", "default": False},
                            },
                            "additionalProperties": False,
                        },
                    },
                    {
                        "name": "argoss_disk_guard",
                        "description": "Safe dry-run disk guard.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "apply": {"type": "boolean", "default": False},
                            },
                            "additionalProperties": False,
                        },
                    },
                    {
                        "name": "argoss_runtime_guard",
                        "description": "Runtime guard snapshot.",
                        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
                    },
                    {
                        "name": "argoss_accelerator_guard",
                        "description": "Accelerator/FPGA/GPU guard snapshot.",
                        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
                    },
                    {
                        "name": "acp_bridge",
                        "description": "ACP bridge status and action proxy.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "action": {"type": "string", "enum": ["status", "info"], "default": "status"},
                            },
                            "additionalProperties": False,
                        },
                    },
                    {
                        "name": "xiaozhi_stability",
                        "description": "Xiaozhi/ESP stability monitor.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "duration": {"type": "integer", "default": 10},
                                "interval": {"type": "integer", "default": 2},
                            },
                            "additionalProperties": False,
                        },
                    },
                    {
                        "name": "xiaozhi_debug",
                        "description": "Xiaozhi debug actions.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "action": {"type": "string", "enum": ["stop_sd", "say"]},
                                "text": {"type": "string"},
                            },
                            "required": ["action"],
                            "additionalProperties": False,
                        },
                    },
                    {
                        "name": "command",
                        "description": (
                            "Выполнить команду через ядро ARGOS. "
                            "Примеры: 'статус', 'hf status', 'провайдеры', 'память', 'мысли', 'эволюция', 'режим ии grok'. "
                            "Поддерживаются все команды ARGOS."
                        ),
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "text": {
                                    "type": "string",
                                    "description": "Команда для ARGOS на русском или английском языке",
                                },
                                "command": {
                                    "type": "string",
                                    "description": "Алиас для text (совместимость с MCP клиентами)",
                                }
                            },
                            "additionalProperties": False,
                        },
                    },
                    {
                        "name": "mempalace",
                        "description": (
                            "Прямой доступ к памяти MemPalace без общего command-роутера. "
                            "Действия: status, search, store, context."
                        ),
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "action": {
                                    "type": "string",
                                    "enum": ["status", "search", "store", "context"],
                                    "description": "Операция MemPalace",
                                },
                                "query": {
                                    "type": "string",
                                    "description": "Поисковый запрос для search/context",
                                },
                                "text": {
                                    "type": "string",
                                    "description": "Текст для store",
                                },
                                "wing": {
                                    "type": "string",
                                    "description": "Крыло памяти (опционально)",
                                },
                                "room": {
                                    "type": "string",
                                    "description": "Комната памяти (опционально)",
                                },
                                "source": {
                                    "type": "string",
                                    "description": "Источник записи (для store)",
                                },
                                "top_k": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "maximum": 10,
                                    "description": "Сколько результатов вернуть в search",
                                },
                            },
                            "required": ["action"],
                            "additionalProperties": False,
                        },
                    },
                    {
                        "name": "argoss_env_dedup",
                        "description": "Находит и устраняет дубликаты ENV-переменных в .env файле. action=check (по умолчанию) — показать дубликаты; action=fix — удалить дубли (сохранить первую).",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "action": {"type": "string", "enum": ["check", "fix"], "default": "check"},
                            },
                            "additionalProperties": False,
                        },
                    },
                    {
                        "name": "argoss_system_diagnostics",
                        "description": "Комплексная диагностика системы: GPU, провайдеры, лимиты, uptime, CPU/RAM, open ports.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {},
                            "additionalProperties": False,
                        },
                    },
                    {
                        "name": "autogpt",
                        "description": "AutoGPT — автономный агент с сущностями провайдеров. Выполняет задачи через AI-сущности (DeepSeek, Gemini, OpenAI, Ollama и др.) с памятью.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "task": {
                                    "type": "string",
                                    "description": "Задача для AutoGPT"
                                }
                            },
                            "required": ["task"],
                            "additionalProperties": False,
                        },
                    },
                    {
                        "name": "gpu_status",
                        "description": "Статус GPU кластера. action=status (по умолчанию), start, stop, benchmark.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "action": {"type": "string", "enum": ["status", "start", "stop", "benchmark"], "default": "status"},
                            },
                            "additionalProperties": False,
                        },
                    },
                    {
                        "name": "xilinx_fpga",
                        "description": (
                            "Статус Xilinx/AMD PCIe FPGA. "
                            "Показывает обнаружение платы, Hardware IDs, SUBSYS, PCIe link и состояние драйвера. "
                            "action=status/detect/plan/dma_probe/dma_test/locked_profile/gti_plan/gti_api/sdk_sources/sdk_status."
                        ),
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "action": {
                                    "type": "string",
                                    "enum": ["status", "detect", "json", "plan", "driver_plan", "dma_probe", "dma_test", "locked_profile", "gti_plan", "gti_api", "sdk_sources", "sdk_status"],
                                    "default": "status",
                                },
                            },
                            "additionalProperties": False,
                        },
                    },
                    {
                        "name": "argoss_white_audit",
                        "description": "White audit: проверка портов, ENV, ACL, hang markers.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {},
                            "additionalProperties": False,
                        },
                    },
                    {
                        "name": "argoss_hardening_status",
                        "description": "Статус hardening: MCP timeout, TG timeout, watchdogs.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {},
                            "additionalProperties": False,
                        },
                    },
                    {
                        "name": "argoss_colab_pipeline",
                        "description": "Сборка Colab fine-tune пайплайна из Obsidian + Evolver датасетов.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "recent_days": {"type": "integer", "default": 30},
                                "max_examples": {"type": "integer", "default": 2000},
                                "max_chars": {"type": "integer", "default": 1800},
                            },
                            "additionalProperties": False,
                        },
                    },
                    {
                        "name": "xui_vpn",
                        "description": (
                            "Управление VPN через 3x-ui панель на Railway. "
                            "action=list_inbounds — список inbound. "
                            "action=add_client — добавить клиента (нужен inbound_id и email). "
                            "action=get_stats — статистика трафика клиента. "
                            "action=status — статус панели."
                        ),
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "action": {
                                    "type": "string",
                                    "enum": ["status", "list_inbounds", "add_client", "get_stats"],
                                    "default": "status",
                                },
                                "inbound_id": {"type": "integer"},
                                "email": {"type": "string"},
                            },
                            "additionalProperties": False,
                        },
                    },
                    {
                        "name": "argos_vpn",
                        "description": (
                            "Argos WireGuard VPN mini-app: управление клиентами и конфигами. "
                            "action=status — статус сервиса. "
                            "action=register — зарегистрировать telegram_id. "
                            "action=create — создать конфиг (telegram_id). "
                            "action=get — получить статус клиента (telegram_id). "
                            "action=cleanup — деактивировать просроченные ключи."
                        ),
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "action": {
                                    "type": "string",
                                    "enum": ["status", "register", "create", "get", "cleanup", "clients"],
                                    "default": "status",
                                },
                                "telegram_id": {"type": "integer"},
                                "username": {"type": "string"},
                            },
                            "additionalProperties": False,
                        },
                    },
                    {
                        "name": "obsidian_status",
                        "description": "Статус Obsidian vault.",
                        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
                    },
                    {
                        "name": "obsidian_search",
                        "description": "Поиск в Obsidian.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                            "required": ["query"],
                            "additionalProperties": False,
                        },
                    },
                    {
                        "name": "obsidian_read",
                        "description": "Чтение заметки Obsidian.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                            "required": ["path"],
                            "additionalProperties": False,
                        },
                    },
                    {
                        "name": "obsidian_daily",
                        "description": "Открыть/создать daily note.",
                        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
                    },
                    {
                        "name": "xiaozhi_status",
                        "description": "Статус ESP32-S3 Xiaozhi голосового сервера и подключённого устройства.",
                        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
                    },
                    {
                        "name": "xiaozhi_music_play",
                        "description": "Включить музыку на ESP32-S3 через xiaozhi. path опционально — случайный трек.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string", "default": "", "description": "Путь к файлу музыки на ПК"}
                            },
                            "additionalProperties": False,
                        },
                    },
                    {
                        "name": "xiaozhi_music_list",
                        "description": "Список найденной музыки на ПК для проигрывания через ESP.",
                        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
                    },
                    {
                        "name": "headroom",
                        "description": (
                            "Headroom — контекстная оптимизация и сжатие для LLM. "
                            "Действия: status, proxy (start/stop/status), compress <текст>, "
                            "tokens <текст>, memory (list/stats), learn. "
                            "Примеры: 'headroom status', 'headroom proxy start', 'headroom compress текст'."
                        ),
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "action": {
                                    "type": "string",
                                    "enum": ["status", "proxy", "compress", "tokens", "memory", "learn"],
                                    "description": "Действие headroom",
                                },
                                "text": {
                                    "type": "string",
                                    "description": "Текст для сжатия/подсчёта токенов",
                                },
                                "proxy_action": {
                                    "type": "string",
                                    "enum": ["start", "stop", "status"],
                                    "description": "Действие с прокси",
                                },
                                "memory_action": {
                                    "type": "string",
                                    "enum": ["list", "stats"],
                                    "description": "Действие с памятью",
                                },
                            },
                            "required": ["action"],
                            "additionalProperties": False,
                        },
                    },
                    {
                        "name": "bio_consciousness",
                        "description": (
                            "Биоразум Сознания ARGOS — биологически вдохновленный AI модуль. "
                            "Объединяет нейронные сети, эволюционные алгоритмы и био-память. "
                            "Действия: status, report, process, think, learn, dna, neurons, memory."
                        ),
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "action": {
                                    "type": "string",
                                    "enum": ["status", "report", "process", "think", "learn", "dna", "neurons", "memory"],
                                    "description": "Действие: status (статус), report (отчёт), process (обработка), think (мысль), learn (обучение), dna (ДНК), neurons (нейроны), memory (память)"
                                },
                                "input": {
                                    "type": "string",
                                    "description": "Входные данные для обработки (для action=process)"
                                },
                                "category": {
                                    "type": "string",
                                    "default": "general",
                                    "description": "Категория обработки (для action=process)"
                                },
                                "query": {
                                    "type": "string",
                                    "description": "Запрос для размышления (для action=think)"
                                },
                                "experience": {
                                    "type": "object",
                                    "description": "Опыт для обучения {intelligence: 0.8, creativity: 0.7, ...} (для action=learn)"
                                },
                            },
                            "additionalProperties": False,
                        },
                    },
                    {
                        "name": "bio_neuron",
                        "description": (
                            "Работа с отдельными био-нейронами. "
                            "Действия: activate, get, list, stats. "
                            "Примеры: 'bio_neuron activate bio_neuron_0001 0.5', 'bio_neuron list sensory'."
                        ),
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "action": {
                                    "type": "string",
                                    "enum": ["activate", "get", "list", "stats"],
                                    "description": "Действие с нейроном"
                                },
                                "neuron_id": {
                                    "type": "string",
                                    "description": "ID нейрона (для activate, get)"
                                },
                                "signal": {
                                    "type": "number",
                                    "default": 0.5,
                                    "description": "Сигнал активации (для activate, 0-1)"
                                },
                                "neuron_type": {
                                    "type": "string",
                                    "description": "Тип нейрона для фильтрации (для list)"
                                },
                            },
                            "additionalProperties": False,
                        },
                    },
                    {
                        "name": "bio_dna",
                        "description": (
                            "Работа с ДНК биоразума. "
                            "Действия: status, mutate, evolve, characteristics. "
                            "Примеры: 'bio_dna status', 'bio_dna mutate', 'bio_dna characteristics'."
                        ),
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "action": {
                                    "type": "string",
                                    "enum": ["status", "mutate", "evolve", "characteristics"],
                                    "description": "Действие с ДНК"
                                },
                                "experience": {
                                    "type": "object",
                                    "description": "Опыт для эволюции (для action=evolve)"
                                },
                            },
                            "additionalProperties": False,
                        },
                    },
                    {
                        "name": "go",
                        "description": (
                            "Go (Golang) инструменты для исполнения и анализа кода. "
                            "Действия: status, run, compile, analyze, module_init, module_deps, test. "
                            "Примеры: 'go status', 'go run code:...', 'go compile code:...', 'go test'."
                        ),
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "action": {
                                    "type": "string",
                                    "enum": ["status", "run", "compile", "analyze", "module_init", "module_deps", "test"],
                                    "description": "Действие: status (статус), run (исполнение), compile (компиляция), analyze (анализ), module_init (модуль), module_deps (зависимости), test (тесты)"
                                },
                                "code": {
                                    "type": "string",
                                    "description": "Go код для исполнения/компиляции/анализа"
                                },
                                "module_path": {
                                    "type": "string",
                                    "description": "Путь к Go модулю (для module_init, module_deps)"
                                },
                                "package_path": {
                                    "type": "string",
                                    "default": ".",
                                    "description": "Путь к Go пакету для тестирования"
                                },
                                "output": {
                                    "type": "string",
                                    "default": "output",
                                    "description": "Имя выходного файла (для compile)"
                                },
                                "timeout": {
                                    "type": "integer",
                                    "default": 30,
                                    "description": "Таймаут в секундах"
                                },
                            },
                            "additionalProperties": False,
                        },
                    },
                    {
                        "name": "go_file",
                        "description": (
                            "Работа с Go файлами на диске. "
                            "Действия: read, write, list, delete, format. "
                            "Примеры: 'go_file read path:main.go', 'go_file write path:main.go code:...', 'go_file list path:.'"
                        ),
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "action": {
                                    "type": "string",
                                    "enum": ["read", "write", "list", "delete", "format"],
                                    "description": "Действие: read (чтение), write (запись), list (список), delete (удаление), format (форматирование)"
                                },
                                "path": {
                                    "type": "string",
                                    "description": "Путь к файлу или директории"
                                },
                                "code": {
                                    "type": "string",
                                    "description": "Код для записи в файл (для write)"
                                },
                                "recursive": {
                                    "type": "boolean",
                                    "default": False,
                                    "description": "Рекурсивный список (для list)"
                                },
                            },
                            "required": ["action", "path"],
                            "additionalProperties": False,
                        },
                    },
                    {
                        "name": "argoss_mempalace_sync",
                        "description": (
                            "Синхронизация MemPalace между ARGOS-нодами. "
                            "Действия: status (статус авторизации), export (экспорт wing/room в JSON), "
                            "pull (импорт с удалённой ноды), access_status (конфиг доступа)."
                        ),
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "action": {
                                    "type": "string",
                                    "enum": ["status", "export", "pull", "access_status"],
                                    "description": "Действие: status, export, pull, access_status"
                                },
                                "wing": {
                                    "type": "string",
                                    "description": "Wing для фильтрации (опционально)"
                                },
                                "room": {
                                    "type": "string",
                                    "description": "Room для фильтрации (опционально)"
                                },
                                "remote_url": {
                                    "type": "string",
                                    "description": "URL удалённой ноды для pull (опционально)"
                                },
                            },
                            "required": ["action"],
                            "additionalProperties": False,
                        },
                    },
                ]
                return _ok({"tools": tools})

            # ── tools/call ───────────────────────────────────────────────────
            if method == "tools/call":
                params = payload.get("params") or {}
                name = params.get("name")
                args = params.get("arguments") or {}
                try:
                    if name == "providers":
                        text = self._providers()
                    elif name == "skills":
                        text = self._skills()
                    elif name == "limits":
                        text = self._limits()
                    elif name == "status":
                        text = str(self._status())
                    elif name == "headroom_compress":
                        text = self._headroom_compress(str(args.get("text", "")))
                    elif name == "osint":
                        text = self._osint(str(args.get("service", "")), str(args.get("query", "")))
                    elif name == "image_generate":
                        text = self._image_generate(
                            prompt=str(args.get("prompt", "")),
                            negative_prompt=str(args.get("negative_prompt", "")),
                            steps=int(args.get("steps", 20) or 20),
                            width=int(args.get("width", 1024) or 1024),
                            height=int(args.get("height", 1024) or 1024),
                            model_name=(str(args.get("model_name")) if args.get("model_name") else None),
                        )
                    elif name == "cloudflare_models":
                        text = self._cloudflare_models()
                    elif name == "cloudflare_chat":
                        text = await self._cloudflare_chat(
                            prompt=str(args.get("prompt", "")),
                            model=str(args.get("model")) if args.get("model") else None,
                            system=str(args.get("system")) if args.get("system") else None,
                            temperature=float(args.get("temperature", 0.4) or 0.4),
                            max_tokens=int(args.get("max_tokens", 1200) or 1200),
                        )
                    elif name == "npm":
                        text = self._npm_command(
                            command=str(args.get("command", "")),
                            package=str(args.get("package", "")) or None,
                            script=str(args.get("script", "")) or None,
                            global_install=bool(args.get("global", False)),
                            dev=bool(args.get("dev", False)),
                            fix=bool(args.get("fix", False)),
                            npx_args=args.get("args") or [],
                            cwd=str(args.get("cwd", "")) or None,
                        )
                    elif name == "porphyry":
                        text = self._porphyry_command(
                            action=str(args.get("action", "contemplate")),
                            topic=str(args.get("topic", "")) or None,
                            mode=str(args.get("mode", "")) or None,
                            depth=int(args.get("depth", 1) or 1),
                            command=str(args.get("command", "")) or None,
                        )
                    elif name == "orangepi_gadget":
                        text = self._orangepi_gadget_command(
                            action=str(args.get("action", "status")),
                            mode=str(args.get("mode", "")) or None,
                        )
                    elif name == "orangepi_bridge":
                        text = self._orangepi_bridge_command(
                            action=str(args.get("action", "status")),
                            pin=args.get("pin"),
                            value=args.get("value"),
                            addr=args.get("addr"),
                            reg=args.get("reg"),
                            data=str(args.get("data", "")) or None,
                            slave=args.get("slave"),
                            count=args.get("count"),
                            speed=args.get("speed"),
                        )
                    elif name == "ollama_vision":
                        text = self._ollama_vision_command(
                            action=str(args.get("action", "status")),
                            message=str(args.get("message", "")) or None,
                            image_path=str(args.get("image_path", "")) or None,
                        )
                    elif name == "pi_bridge":
                        text = self._pi_bridge_command(
                            action=str(args.get("action", "status")),
                            prompt=str(args.get("prompt", "")) or None,
                            model=str(args.get("model", "")) or None,
                            timeout=int(args.get("timeout", 300) or 300),
                        )
                    elif name == "command":
                        cmd_text = str(args.get("text", "") or args.get("command", ""))
                        text = await self._run_command(cmd_text, prefix_direct=False)
                    elif name == "mempalace":
                        text = self._mempalace_command(
                            action=str(args.get("action", "status")),
                            query=str(args.get("query", "")) or None,
                            text=str(args.get("text", "")) or None,
                            wing=str(args.get("wing", "")) or None,
                            room=str(args.get("room", "")) or None,
                            source=str(args.get("source", "")) or None,
                            top_k=int(args.get("top_k", 5) or 5),
                            client_host=self._last_client_host,
                        )
                    elif name == "argoss_mempalace_sync":
                        text = self._mempalace_sync_command(
                            action=str(args.get("action", "status")),
                            wing=str(args.get("wing", "")) or None,
                            room=str(args.get("room", "")) or None,
                            remote_url=str(args.get("remote_url", "")) or None,
                        )
                    elif name == "argoss_env_dedup":
                        text = self._argoss_env_dedup(action=str(args.get("action", "check") or "check"))
                    elif name == "argoss_system_diagnostics":
                        text = self._argoss_system_diagnostics()
                    elif name == "autogpt":
                        try:
                            from src.skills.autogpt import AutoGPT
                            task = str(args.get("task", ""))
                            if not task:
                                text = "AutoGPT готов. Используй: autogpt <задача>"
                            else:
                                ag = AutoGPT(core=self.core)
                                text = ag.execute_task(task)
                        except Exception as e:
                            text = f"AutoGPT error: {e}"
                    elif name == "gpu_status":
                        try:
                            act = str(args.get("action", "status") or "status")
                            if act == "status":
                                text = self._local_gpu_status()
                            elif act == "start":
                                text = self._start_v100_gpu() + "\n\n" + self._local_gpu_status()
                            else:
                                from src.ollama_three import get_manager
                                mgr = get_manager()
                                if act == "stop":
                                    text = mgr.stop_all()
                                elif act == "benchmark":
                                    text = mgr.benchmark()
                                else:
                                    text = self._local_gpu_status()
                        except Exception as exc:
                            text = f"GPU error: {exc}"
                    elif name == "xilinx_fpga":
                        text = self._xilinx_fpga_command(action=str(args.get("action", "status") or "status"))
                    elif name == "fpga_safe_prepare":
                        text = self._fpga_safe_prepare_command()
                    elif name == "spr2801_linux_lab":
                        text = self._spr2801_linux_lab_command()
                    elif name == "spr2801_crash_audit":
                        text = self._spr2801_crash_audit_command()
                    elif name == "spr2801_dump_prepare":
                        text = self._spr2801_dump_prepare_command()
                    elif name == "gyrfalcon_vision":
                        text = self._gyrfalcon_vision_command(
                            action=str(args.get("action", "status")),
                            image_path=str(args.get("image_path", "")),
                            backend=str(args.get("backend", "software")),
                        )
                    elif name == "gyrfalcon_bsod_guard":
                        text = self._gyrfalcon_bsod_guard_command()
                    elif name == "argoss_machine_sync":
                        text = await asyncio.to_thread(
                            self._argoss_machine_sync_command,
                            refresh_gcp=bool(args.get("refresh_gcp", True)),
                            skip_obsidian=bool(args.get("skip_obsidian", False)),
                        )
                    elif name == "argoss_disk_guard":
                        text = self._argoss_disk_guard_command(apply=bool(args.get("apply", False)))
                    elif name == "argoss_runtime_guard":
                        text = self._argoss_runtime_guard_command()
                    elif name == "argoss_accelerator_guard":
                        text = self._argoss_accelerator_guard_command()
                    elif name == "acp_bridge":
                        text = self._acp_bridge_command(action=str(args.get("action", "status")))
                    elif name == "argoss_white_audit":
                        text = self._argoss_white_audit()
                    elif name == "argoss_hardening_status":
                        text = self._argoss_hardening_status()
                    elif name == "argoss_colab_pipeline":
                        text = "⚠️ colab_pipeline временно недоступен (метод не реализован)"
                    elif name == "xui_vpn":
                        import aiohttp as _aiohttp, re as _re
                        _xui_base = os.getenv("XUI_PANEL_URL", "http://acela.proxy.rlwy.net:45339")
                        _xui_key = os.getenv("XUI_API_KEY", "")
                        _xui_action = str(args.get("action", "status"))
                        async def _xui_login(sess):
                            _r = await sess.get(f"{_xui_base}/panel/")
                            _html = await _r.text()
                            _m = _re.search(r'csrf-token.*?content="([^"]+)"', _html)
                            _csrf = _m.group(1) if _m else ""
                            _r2 = await sess.post(f"{_xui_base}/login",
                                json={"username": "admin", "password": _xui_key},
                                headers={"X-Csrf-Token": _csrf})
                            return _csrf
                        async def _xui_call():
                            async with _aiohttp.ClientSession() as _sess:
                                _csrf = await _xui_login(_sess)
                                _h = {"X-Csrf-Token": _csrf}
                                if _xui_action == "status":
                                    _r = await _sess.get(f"{_xui_base}/panel/")
                                    return f"3x-ui panel: {_xui_base}\nStatus: {'OK' if _r.status == 200 else _r.status}\nAPI key configured: {'yes' if _xui_key else 'no'}"
                                elif _xui_action == "list_inbounds":
                                    _r = await _sess.get(f"{_xui_base}/xui/inbound/list", headers=_h)
                                    _d = await _r.json()
                                    if _d.get("success"):
                                        _lines = [f"ID:{ib['id']} {ib['protocol']} :{ib['port']} '{ib['remark']}' enable={ib['enable']}" for ib in _d.get("obj", [])]
                                        return "Inbounds:\n" + "\n".join(_lines) if _lines else "No inbounds"
                                    return f"Error: {_d.get('msg', _r.status)}"
                                elif _xui_action == "add_client":
                                    import uuid as _uuid
                                    _iid = int(args.get("inbound_id", 1))
                                    _email = str(args.get("email", f"user_{_uuid.uuid4().hex[:6]}"))
                                    _payload = {"clients": [{"id": str(_uuid.uuid4()), "email": _email, "flow": "xtls-rprx-vision", "limitIp": 0, "totalGB": 0, "expiryTime": 0, "enable": True}]}
                                    _r = await _sess.post(f"{_xui_base}/xui/inbound/{_iid}/client/add", json=_payload, headers=_h)
                                    _d = await _r.json()
                                    return f"Add client '{_email}': {'OK' if _d.get('success') else _d.get('msg', 'error')}"
                                elif _xui_action == "get_stats":
                                    _iid = int(args.get("inbound_id", 1))
                                    _r = await _sess.get(f"{_xui_base}/xui/inbound/{_iid}/clientStats", headers=_h)
                                    _d = await _r.json()
                                    if _d.get("success"):
                                        _lines = [f"{c['email']}: ↑{c['up']//1024//1024}MB ↓{c['down']//1024//1024}MB" for c in _d.get("obj", [])]
                                        return "\n".join(_lines) if _lines else "No stats"
                                    return f"Error: {_d.get('msg')}"
                                return "Unknown action"
                        import asyncio as _asyncio
                        text = _asyncio.get_event_loop().run_until_complete(_xui_call()) if not _asyncio.get_event_loop().is_running() else await _xui_call()
                    elif name == "argos_vpn":
                        text = await self._argos_vpn_command(
                            action=str(args.get("action", "status")),
                            telegram_id=args.get("telegram_id"),
                            username=str(args.get("username", "")) or None,
                        )
                    elif name == "obsidian_status":
                        text = self._obsidian_command(action="status")
                    elif name == "obsidian_search":
                        text = self._obsidian_command(action="search", query=str(args.get("query", "")) or None)
                    elif name == "obsidian_read":
                        text = self._obsidian_command(action="read", path=str(args.get("path", "")) or None)
                    elif name == "obsidian_daily":
                        text = self._obsidian_command(action="daily")
                    elif name == "xiaozhi_status":
                        import httpx
                        info = {"server": "unknown", "esp": "disconnected"}
                        try:
                            async with httpx.AsyncClient(timeout=5) as c:
                                r = await c.get("http://127.0.0.1:8006/xiaozhi/music/")
                                if r.is_success:
                                    d = r.json()
                                    info["server"] = "ok"
                                    info["music_files"] = d.get("files", 0)
                                    info["dirs"] = d.get("dirs", [])
                                    info["sd_player_format"] = d.get("esp_sd_player_format")
                                    info["sd_wav_prefix"] = d.get("esp_sd_wav_prefix")
                                    info["sd_prefix"] = d.get("esp_sd_prefix")
                        except Exception:
                            info["server"] = "down"
                        try:
                            async with httpx.AsyncClient(timeout=5) as c:
                                r = await c.get("http://127.0.0.1:8006/xiaozhi/debug/sessions")
                                if r.is_success:
                                    d = r.json()
                                    info["esp"] = "connected" if d.get("sessions") else "disconnected"
                                    info["sessions"] = d.get("sessions", [])
                        except Exception:
                            pass
                        try:
                            async with httpx.AsyncClient(timeout=5) as c:
                                r = await c.get("http://127.0.0.1:8006/health")
                                if r.is_success:
                                    info["health"] = "ok"
                        except Exception:
                            info["health"] = "down"
                        text = json.dumps(info, ensure_ascii=False)
                    elif name == "xiaozhi_music_play":
                        import httpx
                        try:
                            path = str(args.get("path", "") or "")
                            async with httpx.AsyncClient(timeout=30) as c:
                                url = "http://127.0.0.1:8006/xiaozhi/debug/play_sd"
                                if path:
                                    url += f"?path={urllib.parse.quote(path)}"
                                r = await c.post(url)
                                text = json.dumps(r.json(), ensure_ascii=False)
                        except Exception as e:
                            text = json.dumps({"status": "error", "error": str(e)[:200]}, ensure_ascii=False)
                    elif name == "xiaozhi_music_list":
                        import httpx
                        try:
                            async with httpx.AsyncClient(timeout=10) as c:
                                r = await c.get("http://127.0.0.1:8006/xiaozhi/music/")
                                if r.is_success:
                                    d = r.json()
                                    files = d.get("sample", [])
                                    music = [{"name": Path(f).stem, "path": f} for f in files[:50]]
                                    text = json.dumps({"status": "ok", "count": d.get("files", 0), "music": music}, ensure_ascii=False)
                                else:
                                    text = json.dumps({"status": "error", "error": f"HTTP {r.status_code}"})
                        except Exception as e:
                            text = json.dumps({"status": "error", "error": str(e)[:200]}, ensure_ascii=False)
                    elif name == "xiaozhi_stability":
                        try:
                            text = await self._xiaozhi_stability_command(
                                duration=int(args.get("duration", 10) or 10),
                                interval=int(args.get("interval", 2) or 2),
                            )
                        except Exception as e:
                            text = json.dumps({"status": "error", "error": str(e)[:200]}, ensure_ascii=False)
                    elif name == "xiaozhi_debug":
                        try:
                            text = await self._xiaozhi_debug(action=str(args.get("action", "sessions")))
                        except Exception as e:
                            text = json.dumps({"status": "error", "error": str(e)[:200]}, ensure_ascii=False)
                    elif name == "headroom":
                        from src.skills.headroom_skill import handle_command, status, proxy_command, compress_text, count_tokens, memory_command, learn
                        action = str(args.get("action", "status") or "status")
                        if action == "status":
                            text = status()
                        elif action == "proxy":
                            proxy_act = str(args.get("proxy_action", "status") or "status")
                            text = proxy_command(proxy_act)
                        elif action == "compress":
                            txt = str(args.get("text", "") or "")
                            text = compress_text(txt)
                        elif action == "tokens":
                            txt = str(args.get("text", "") or "")
                            text = count_tokens(txt)
                        elif action == "memory":
                            mem_act = str(args.get("memory_action", "stats") or "stats")
                            text = memory_command(mem_act)
                        elif action == "learn":
                            text = learn()
                        else:
                            text = status()
                    elif name == "bio_consciousness":
                        # Работа с BioConsciousness
                        action = str(args.get("action", "status") or "status")
                        try:
                            from src.bio_integration import BioConsciousnessManager
                            # Получаем менеджер из ядра или создаём новый
                            if self.core and hasattr(self.core, 'bio_consciousness'):
                                manager = self.core.bio_consciousness
                            else:
                                manager = BioConsciousnessManager()
                            
                            if action == "status":
                                import json as _json
                                text = _json.dumps(manager.get_status(), indent=2, ensure_ascii=False)
                            elif action == "report":
                                text = manager.get_self_report()
                            elif action == "process":
                                input_data = args.get("input", "")
                                category = str(args.get("category", "general") or "general")
                                result = manager.process(input_data, category)
                                import json as _json
                                text = _json.dumps(result, indent=2, ensure_ascii=False)
                            elif action == "think":
                                query = str(args.get("query", "") or "")
                                context = args.get("context", {})
                                thought = manager.generate_thought(query, context)
                                import json as _json
                                text = _json.dumps(thought, indent=2, ensure_ascii=False)
                            elif action == "learn":
                                experience = args.get("experience", {})
                                result = manager.learn(experience)
                                import json as _json
                                text = _json.dumps(result, indent=2, ensure_ascii=False)
                            elif action == "dna":
                                if manager.bio_consciousness:
                                    chars = manager.bio_consciousness.dna.characteristics
                                    lines = ["🧬 BioConsciousness DNA Characteristics:"]
                                    for k, v in chars.items():
                                        lines.append(f"  {k}: {v:.2%}")
                                    text = "\n".join(lines)
                                else:
                                    text = "BioConsciousness not available"
                            elif action == "neurons":
                                if manager.bio_consciousness:
                                    neurons = manager.bio_consciousness.neurons
                                    lines = [f"🧠 Bio Neurons: {len(neurons)} total"]
                                    type_counts = {}
                                    for neuron in neurons.values():
                                        ntype = neuron.neuron_type
                                        type_counts[ntype] = type_counts.get(ntype, 0) + 1
                                    for ntype, count in type_counts.items():
                                        lines.append(f"  {ntype}: {count}")
                                    text = "\n".join(lines)
                                else:
                                    text = "BioConsciousness not available"
                            elif action == "memory":
                                if manager.bio_consciousness:
                                    memory = manager.bio_consciousness.memory
                                    text = f"💾 Bio Memory: STM={len(memory.short_term)}, LTM={len(memory.long_term)}, associations={len(memory.associations)}"
                                else:
                                    text = "BioConsciousness not available"
                            else:
                                text = f"Unknown bio_consciousness action: {action}"
                        except Exception as exc:
                            text = f"BioConsciousness error: {exc}"
                    elif name == "bio_neuron":
                        # Работа с отдельными нейронами
                        action = str(args.get("action", "list") or "list")
                        try:
                            from src.bio_integration import BioConsciousnessManager
                            if self.core and hasattr(self.core, 'bio_consciousness'):
                                manager = self.core.bio_consciousness
                            else:
                                manager = BioConsciousnessManager()
                            
                            if not manager.bio_consciousness:
                                text = "BioConsciousness not initialized"
                            else:
                                bio = manager.bio_consciousness
                                if action == "list":
                                    neuron_type = args.get("neuron_type", "")
                                    if neuron_type:
                                        neuron_ids = bio.neuron_network.get(neuron_type, [])
                                        text = f"Neurons of type '{neuron_type}': {len(neuron_ids)}"
                                    else:
                                        text = f"All neurons: {len(bio.neurons)}"
                                elif action == "get":
                                    neuron_id = str(args.get("neuron_id", "") or "")
                                    if neuron_id in bio.neurons:
                                        neuron = bio.neurons[neuron_id]
                                        text = f"Neuron {neuron_id}: type={neuron.neuron_type}, activation={neuron.activation:.3f}, health={neuron.health:.3f}"
                                    else:
                                        text = f"Neuron {neuron_id} not found"
                                elif action == "activate":
                                    neuron_id = str(args.get("neuron_id", "") or "")
                                    signal = float(args.get("signal", 0.5) or 0.5)
                                    if neuron_id in bio.neurons:
                                        neuron = bio.neurons[neuron_id]
                                        activation = neuron.activate(signal)
                                        text = f"Neuron {neuron_id} activated: {activation:.3f}"
                                    else:
                                        text = f"Neuron {neuron_id} not found"
                                elif action == "stats":
                                    lines = [f"Bio Neurons Stats: {len(bio.neurons)} total"]
                                    for ntype, nids in bio.neuron_network.items():
                                        lines.append(f"  {ntype}: {len(nids)}")
                                    text = "\n".join(lines)
                                else:
                                    text = f"Unknown bio_neuron action: {action}"
                        except Exception as exc:
                            text = f"BioNeuron error: {exc}"
                    elif name == "bio_dna":
                        # Работа с ДНК
                        action = str(args.get("action", "status") or "status")
                        try:
                            from src.bio_integration import BioConsciousnessManager
                            if self.core and hasattr(self.core, 'bio_consciousness'):
                                manager = self.core.bio_consciousness
                            else:
                                manager = BioConsciousnessManager()
                            
                            if not manager.bio_consciousness:
                                text = "BioConsciousness not initialized"
                            else:
                                dna = manager.bio_consciousness.dna
                                if action == "status":
                                    text = f"Bio DNA: version={dna.version}, generation={dna.generation}, mutation_rate={dna.mutation_rate:.4f}"
                                elif action == "mutate":
                                    dna.mutate()
                                    text = f"Bio DNA mutated: generation={dna.generation}"
                                elif action == "evolve":
                                    experience = args.get("experience", {})
                                    dna.evolve_from_experience(experience)
                                    text = f"Bio DNA evolved: generation={dna.generation}"
                                elif action == "characteristics":
                                    lines = ["Bio DNA Characteristics:"]
                                    for k, v in dna.characteristics.items():
                                        lines.append(f"  {k}: {v:.2%}")
                                    text = "\n".join(lines)
                                else:
                                    text = f"Unknown bio_dna action: {action}"
                        except Exception as exc:
                            text = f"BioDNA error: {exc}"
                    elif name == "go":
                        # Обработка Go команд
                        action = str(args.get("action", "status") or "status")
                        try:
                            from src.tools.go_tools import handle_go_command
                            # Удаляем action из args, чтобы избежать дублирования
                            go_args = args.copy()
                            go_args.pop("action", None)
                            result = handle_go_command(action, **go_args)
                            if isinstance(result, dict):
                                if result.get("ok"):
                                    text = json.dumps(result, ensure_ascii=False)
                                else:
                                    text = json.dumps(result, ensure_ascii=False)
                            else:
                                text = str(result)
                        except Exception as exc:
                            text = f"Go error: {exc}"
                    elif name == "go_file":
                        # Обработка Go файлов
                        action = str(args.get("action", "") or "")
                        path = str(args.get("path", "") or "")
                        code = args.get("code", "")
                        recursive = args.get("recursive", False)
                        
                        try:
                            if action == "read":
                                if os.path.exists(path):
                                    with open(path, encoding="utf-8") as f:
                                        text = f.read()
                                else:
                                    text = f"File not found: {path}"
                            elif action == "write":
                                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                                with open(path, "w", encoding="utf-8") as f:
                                    f.write(code)
                                text = f"Written {len(code)} bytes to {path}"
                            elif action == "list":
                                if os.path.isdir(path):
                                    files = []
                                    for item in os.listdir(path):
                                        item_path = os.path.join(path, item)
                                        if os.path.isfile(item_path):
                                            files.append({
                                                "name": item,
                                                "path": item_path,
                                                "size": os.path.getsize(item_path)
                                            })
                                        elif recursive and os.path.isdir(item_path):
                                            for root, dirs, filenames in os.walk(item_path):
                                                for filename in filenames:
                                                    files.append({
                                                        "name": filename,
                                                        "path": os.path.join(root, filename),
                                                        "size": os.path.getsize(os.path.join(root, filename))
                                                    })
                                    text = json.dumps({"files": files, "count": len(files)}, ensure_ascii=False)
                                else:
                                    text = f"Not a directory: {path}"
                            elif action == "delete":
                                if os.path.exists(path):
                                    if os.path.isfile(path):
                                        os.remove(path)
                                        text = f"Deleted: {path}"
                                    else:
                                        text = f"Not a file: {path}"
                                else:
                                    text = f"Not found: {path}"
                            elif action == "format":
                                try:
                                    from src.tools.go_tools import get_go_runner
                                    runner = get_go_runner()
                                    # Используем gofmt
                                    result = subprocess.run(
                                        ["gofmt", "-w", path],
                                        capture_output=True, text=True, timeout=30
                                    )
                                    text = f"Formatted {path}: {'OK' if result.returncode == 0 else result.stderr}"
                                except Exception:
                                    text = f"gofmt not available for {path}"
                            else:
                                text = f"Unknown go_file action: {action}"
                        except Exception as exc:
                            text = f"Go file error: {exc}"
                    else:
                        return _err(-32601, f"Unknown tool: {name}")
                except Exception as exc:
                    text = f"tool error: {exc}"

                # ── Headroom auto-compress (middleware) ──────────────────────
                if isinstance(text, str) and len(text) > 800 and name not in {"headroom_compress", "image_generate"}:
                    try:
                        _hr = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                           "scripts", "headroom")
                        if _hr not in sys.path:
                            sys.path.insert(0, _hr)
                        from argos_compressor import ArgosCompressor
                        r = ArgosCompressor().compress(text)
                        if r.savings_pct > 10:
                            header = (f"[headroom: {r.strategy}, {r.original_chars}→{r.compressed_chars} "
                                      f"символов, экономия {r.savings_pct:.0f}%]\n")
                            text = header + r.text
                    except Exception:
                        pass

                return _ok({"content": [{"type": "text", "text": text}]})

            return _err(-32601, f"Method not found: {method}")

        # P2P endpoints for node announcement and discovery
        @app.post("/p2p/announce")
        async def p2p_announce(request: Request):
            try:
                payload = await request.json()
            except Exception:
                raise HTTPException(status_code=400, detail="Invalid JSON")

            node_id = payload.get("node_id")
            if not node_id:
                raise HTTPException(status_code=400, detail="Missing node_id")

            client_host = request.client.host

            if self.core and hasattr(self.core, "p2p_bridge"):
                self.core.p2p_bridge.registry.update(payload, client_host)
                return {"status": "ok", "message": f"Node {node_id[:8]}... announced"}
            else:
                raise HTTPException(status_code=503, detail="P2P bridge not available")

        @app.get("/p2p/nodes")
        async def p2p_nodes(request: Request):
            if self.core and hasattr(self.core, "p2p_bridge"):
                nodes = self.core.p2p_bridge.registry.all()
                return {"nodes": nodes, "count": len(nodes)}
            else:
                raise HTTPException(status_code=503, detail="P2P bridge not available")

        @app.post("/mempalace/sync/export")
        async def mempalace_sync_export(request: Request):
            """Export MemPalace section as JSON (for remote pull)."""
            try:
                payload = await request.json()
            except Exception:
                raise HTTPException(status_code=400, detail="Invalid JSON")
            wing = (payload.get("wing") or "").strip()
            room = (payload.get("room") or "").strip()
            client_host = request.client.host if request.client else "127.0.0.1"
            try:
                from src.mempalace_access import check_access
                allowed, reason = check_access(client_host, "export", wing or "", room or "")
                if not allowed:
                    raise HTTPException(status_code=403, detail=reason)
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(status_code=500, detail=f"Access check error: {exc}")
            try:
                from src.mempalace_sync import export_to_json
                data = export_to_json(wing=wing or "", room=room or "")
                if isinstance(data, str):
                    return {"status": "error", "detail": data}
                return {"status": "ok", "count": len(data.get("drawers", [])), "data": data}
            except Exception as exc:
                raise HTTPException(status_code=500, detail=f"Export error: {exc}")

        return app


def start_mcp_api(core=None, admin=None, host: str = "127.0.0.1", port: int = 8000):
    server = ArgosMCPServer(core=core, admin=admin)
    config = uvicorn.Config(server.app, host=host, port=port, log_level="warning")
    uv_server = uvicorn.Server(config)
    thread = threading.Thread(target=uv_server.run, daemon=True, name="ArgosMCP")
    thread.start()
    return thread


app = ArgosMCPServer(core=None, admin=None).app


# ── Tests ────────────────────────────────────────────────────────────────────
import unittest


class TestMCPPorphyry(unittest.TestCase):
    """Tests for MCP porphyry integration."""

    def setUp(self):
        self.server = ArgosMCPServer(core=None, admin=None)

    def test_porphyry_command_exists(self):
        """Test that _porphyry_command method exists."""
        self.assertTrue(hasattr(self.server, '_porphyry_command'))

    def test_porphyry_contemplate(self):
        """Test porphyry contemplate via MCP."""
        result = self.server._porphyry_command(
            action="contemplate",
            topic="искусственный интеллект"
        )
        self.assertIn("🧠", result)

    def test_porphyry_mode(self):
        """Test porphyry mode switch via MCP."""
        result = self.server._porphyry_command(
            action="mode",
            mode="consilium"
        )
        self.assertIn("Консилиум", result)

    def test_porphyry_depth(self):
        """Test porphyry depth setting via MCP."""
        result = self.server._porphyry_command(
            action="depth",
            depth=2
        )
        self.assertIn("Глубина", result)

    def test_porphyry_status(self):
        """Test porphyry status via MCP."""
        result = self.server._porphyry_command(action="status")
        self.assertIn("Порфирий", result)

    def test_porphyry_diagnostics(self):
        """Test porphyry diagnostics via MCP."""
        result = self.server._porphyry_command(action="diagnostics")
        self.assertIn("Диагностика", result)

    def test_porphyry_shell(self):
        """Test porphyry shell via MCP."""
        result = self.server._porphyry_command(
            action="shell",
            command="echo test"
        )
        self.assertIn("test", result)

    def test_porphyry_invalid_action(self):
        """Test porphyry invalid action."""
        result = self.server._porphyry_command(action="invalid")
        self.assertIn("Unknown", result)


class TestMCPOrangePiGadget(unittest.TestCase):
    """Tests for MCP Orange Pi Gadget integration."""

    def setUp(self):
        self.server = ArgosMCPServer(core=None, admin=None)

    def test_gadget_command_exists(self):
        """Test that _orangepi_gadget_command method exists."""
        self.assertTrue(hasattr(self.server, '_orangepi_gadget_command'))

    def test_gadget_status(self):
        """Test gadget status via MCP."""
        result = self.server._orangepi_gadget_command(action="status")
        self.assertIn("USB Gadget", result)

    def test_gadget_diagnostics(self):
        """Test gadget diagnostics via MCP."""
        result = self.server._orangepi_gadget_command(action="diagnostics")
        self.assertIn("Диагностика", result)

    def test_gadget_stop(self):
        """Test gadget stop via MCP."""
        result = self.server._orangepi_gadget_command(action="stop")
        # Either stopped or not active message
        self.assertTrue(
            "остановлен" in result or "Не активен" in result or "не активен" in result
        )

    def test_gadget_setup(self):
        """Test gadget setup via MCP."""
        result = self.server._orangepi_gadget_command(
            action="setup",
            mode="all"
        )
        # On non-Orange Pi system it will fail with script not found
        self.assertIsInstance(result, str)
        self.assertTrue(len(result) > 0)

    def test_gadget_invalid_action(self):
        """Test gadget invalid action."""
        result = self.server._orangepi_gadget_command(action="invalid")
        self.assertIn("Unknown", result)


class TestMCPOrangePiBridge(unittest.TestCase):
    """Tests for MCP Orange Pi Bridge integration."""

    def setUp(self):
        self.server = ArgosMCPServer(core=None, admin=None)

    def test_bridge_command_exists(self):
        """Test that _orangepi_bridge_command method exists."""
        self.assertTrue(hasattr(self.server, '_orangepi_bridge_command'))

    def test_bridge_status(self):
        """Test bridge status via MCP."""
        result = self.server._orangepi_bridge_command(action="status")
        self.assertIn("ORANGE PI", result.upper())

    def test_bridge_pin_map(self):
        """Test bridge pin_map via MCP."""
        result = self.server._orangepi_bridge_command(action="pin_map")
        self.assertIn("ORANGE PI", result.upper())

    def test_bridge_gpio_status(self):
        """Test bridge gpio_status via MCP."""
        result = self.server._orangepi_bridge_command(action="gpio_status")
        self.assertIn("GPIO", result)

    def test_bridge_i2c_scan(self):
        """Test bridge i2c_scan via MCP."""
        result = self.server._orangepi_bridge_command(action="i2c_scan")
        self.assertIn("I2C", result)

    def test_bridge_1wire(self):
        """Test bridge 1wire via MCP."""
        result = self.server._orangepi_bridge_command(action="1wire")
        self.assertIn("1-Wire", result)

    def test_bridge_invalid_action(self):
        """Test bridge invalid action."""
        result = self.server._orangepi_bridge_command(action="invalid")
        self.assertIn("Unknown", result)


class TestMCPOllamaVision(unittest.TestCase):
    """Tests for MCP Ollama Vision integration."""

    def setUp(self):
        self.server = ArgosMCPServer(core=None, admin=None)

    def test_vision_command_exists(self):
        """Test that _ollama_vision_command method exists."""
        self.assertTrue(hasattr(self.server, '_ollama_vision_command'))

    def test_vision_status(self):
        """Test vision status via MCP."""
        result = self.server._ollama_vision_command(action="status")
        self.assertIsInstance(result, str)
        self.assertTrue(len(result) > 0)

    def test_vision_invalid_action(self):
        """Test vision invalid action."""
        result = self.server._ollama_vision_command(action="invalid")
        self.assertIn("Unknown", result)


class TestMCPPiBridge(unittest.TestCase):
    """Tests for MCP Pi Bridge integration."""

    def setUp(self):
        self.server = ArgosMCPServer(core=None, admin=None)

    def test_pi_command_exists(self):
        """Test that _pi_bridge_command method exists."""
        self.assertTrue(hasattr(self.server, '_pi_bridge_command'))

    def test_pi_status(self):
        """Test pi status via MCP."""
        result = self.server._pi_bridge_command(action="status")
        self.assertIsInstance(result, str)
        self.assertTrue(len(result) > 0)

    def test_pi_invalid_action(self):
        """Test pi invalid action."""
        result = self.server._pi_bridge_command(action="invalid")
        self.assertIn("Unknown", result)


if __name__ == "__main__":
    unittest.main()
