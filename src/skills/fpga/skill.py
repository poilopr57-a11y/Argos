"""
fpga/skill.py — FPGA Skill для ARGOS (Gyrfalcon 2803S / Xilinx Artix-7 XDMA).

Живой доступ к плате через рабочий драйвер (oem46 AXIPCIE v1.36+) поверх
connectivity.xilinx_fpga.XilinxFPGA (BAR-чтение через CreateFile+ReadFile).

Команды (в тексте сообщения или через execute):
  fpga                 — статус (PnP + DMA + сигнатура XDMA)
  fpga bar             — карта control-BAR (все XDMA-субмодули) + user-сигнатура
  fpga read <node> <off> [len]  — точечное чтение BAR (node: control|user|c2h_0...)
  fpga write <node> <off> <hex> — PIO-запись с readback (только node=user, guardrail)
  fpga monitor         — снимок здоровья (для периодического опроса)
  fpga heal            — self-healing: re-enable устройства если Error/Disabled
  fpga plan            — рекомендации по драйверу

Логирует снимки/события в Obsidian vault (T2O), best-effort.
"""

from __future__ import annotations

import os
import json
import datetime
from pathlib import Path
from typing import Any, Optional

SKILL_NAME = "fpga"
SKILL_DESCRIPTION = "FPGA Gyrfalcon/Xilinx: живой статус, BAR-чтение, self-healing"
TRIGGERS = ["fpga", "плис", "xilinx", "грифон", "gyrfalcon", "плата плис"]

_VEN_DEV = "VEN_10EE&DEV_7022"


def _fpga():
    """Ленивая загрузка XilinxFPGA (Windows ctypes-модуль)."""
    try:
        from connectivity.xilinx_fpga import XilinxFPGA
        return XilinxFPGA()
    except Exception:
        try:
            import sys
            sys.path.insert(0, os.path.join(os.getcwd(), "src"))
            from connectivity.xilinx_fpga import XilinxFPGA
            return XilinxFPGA()
        except Exception:
            return None


def _vault() -> Optional[Path]:
    p = (os.getenv("ARGOS_OBSIDIAN_VAULT_PATH")
         or os.getenv("OBSIDIAN_VAULT_PATH") or r"F:\debug\аргос")
    try:
        vp = Path(p)
        return vp if vp.exists() else None
    except Exception:
        return None


def _obsidian_log(title: str, body: str) -> bool:
    """Append-лог в vault/FPGA/fpga_log.md (T2O). Best-effort."""
    vp = _vault()
    if not vp:
        return False
    try:
        d = vp / "FPGA"
        d.mkdir(parents=True, exist_ok=True)
        f = d / "fpga_log.md"
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with f.open("a", encoding="utf-8") as fh:
            fh.write(f"\n## {stamp} — {title}\n\n{body}\n")
        return True
    except Exception:
        return False


def setup(core=None):
    return None


def teardown():
    return None


# ── основные операции ─────────────────────────────────────────────────────

def snapshot() -> dict[str, Any]:
    """Снимок здоровья FPGA — для monitor/status. Реальные данные."""
    f = _fpga()
    snap: dict[str, Any] = {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "module_ok": f is not None,
        "present": False, "pnp_status": None, "problem": None,
        "dma_access": False, "control_id": None, "xdma_signature": False,
        "user_sig": None, "healthy": False,
    }
    if not f:
        snap["note"] = "XilinxFPGA модуль недоступен (не Windows?)"
        return snap
    det = f.detect()
    dev = (det.get("devices") or [{}])[0]
    snap["present"] = bool(det.get("ok") and dev)
    snap["pnp_status"] = dev.get("status")
    snap["problem"] = dev.get("problem") or None
    snap["instance_id"] = dev.get("instance_id")
    # ВАЖНО: живое чтение BAR (MMIO) ТОЛЬКО когда PnP-статус == OK.
    # Если устройство в Error/Disabled (или линк платы просел под "OK"-устройством),
    # ReadFile в XDMA.sys разыменует мёртвый MMIO-указатель → BugCheck 0x50
    # PAGE_FAULT_IN_NONPAGED_AREA (XDMA не обрабатывает surprise-removal).
    # Доказано дампом 13.06: python.exe→NtReadFile→XDMA+0x2ea1→KeBugCheckEx, 3 краша подряд.
    if snap["present"] and snap["pnp_status"] == "OK":
        probe = f.dma_probe()
        snap["dma_access"] = bool(probe.get("interface_registered"))
        snap["control_id"] = probe.get("control_id_hex")
        snap["xdma_signature"] = bool(probe.get("xdma_signature_ok"))
        u = f.dma_read("user", 0, 8)
        if u:
            snap["user_sig"] = u.decode("ascii", "replace")
    elif snap["present"]:
        snap["note"] = (f"pnp_status={snap['pnp_status']} != OK → live-чтение BAR "
                        "пропущено (защита от XDMA surprise-removal BSOD 0x50)")
    snap["healthy"] = bool(snap["present"]
                           and (snap["pnp_status"] == "OK")
                           and snap["xdma_signature"])
    return snap


def heal() -> dict[str, Any]:
    """Self-healing: если устройство в Error/Disabled — попытаться re-enable
    через PowerShell Enable-PnpDevice (нужны права админа).
    ВАЖНО: перезагрузка bitstream требует JTAG — здесь только восстановление
    PnP-привязки драйвера."""
    import subprocess
    snap = snapshot()
    out: dict[str, Any] = {"action": "none", "before": snap, "after": None,
                           "elevated_needed": False}
    if snap.get("healthy"):
        out["action"] = "noop"
        out["note"] = "устройство здорово, лечение не требуется"
        return out
    if not snap.get("present"):
        out["note"] = "устройство не обнаружено на шине (питание/PCIe?)"
        return out
    # device present но не healthy → пробуем enable
    ps = (
        "$d = Get-PnpDevice | Where-Object { $_.InstanceId -like '*"
        + _VEN_DEV + "*' }; "
        "if ($d) { Enable-PnpDevice -InstanceId $d.InstanceId -Confirm:$false "
        "-ErrorAction SilentlyContinue }; "
        "$d2 = Get-PnpDevice | Where-Object { $_.InstanceId -like '*"
        + _VEN_DEV + "*' }; "
        "Write-Output $d2.Status"
    )
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=30)
        out["action"] = "enable_attempt"
        out["ps_out"] = (r.stdout or "").strip()
        if "access" in (r.stderr or "").lower() or "denied" in (r.stderr or "").lower():
            out["elevated_needed"] = True
    except Exception as e:
        out["error"] = str(e)
    out["after"] = snapshot()
    _obsidian_log("SELF-HEAL", "```json\n"
                  + json.dumps(out, ensure_ascii=False, indent=1) + "\n```")
    return out


# ── рендер для чата ────────────────────────────────────────────────────────

def _render_status() -> str:
    s = snapshot()
    icon = "🟢" if s["healthy"] else ("🟡" if s["present"] else "🔴")
    _obsidian_log("STATUS", "```json\n"
                  + json.dumps(s, ensure_ascii=False, indent=1) + "\n```")
    return "\n".join([
        f"{icon} FPGA (Gyrfalcon 2803S / Artix-7)",
        f"present: {s['present']}  pnp: {s['pnp_status']}  problem: {s['problem'] or 'none'}",
        f"dma_access: {s['dma_access']}  control_id: {s['control_id']}",
        f"xdma_sig: {'OK ✓' if s['xdma_signature'] else 'нет'}  user: {s['user_sig']}",
        f"healthy: {s['healthy']}",
    ])


def _render_bar() -> str:
    f = _fpga()
    if not f:
        return "❌ XilinxFPGA модуль недоступен"
    bm = f.bar_map()
    if not bm.get("available"):
        return f"🔴 BAR недоступен: {bm.get('note')}"
    lines = ["🗺️ FPGA BAR MAP", f"note: {bm.get('note')}",
             f"user: {(bm.get('user') or {}).get('ascii', '-')}", ""]
    for name, v in (bm.get("control") or {}).items():
        lines.append(f"  {name:<12} {v['offset']}  {v['id']}")
    return "\n".join(lines)


def _render_read(parts: list[str]) -> str:
    f = _fpga()
    if not f:
        return "❌ XilinxFPGA модуль недоступен"
    node = parts[0] if parts else "control"
    try:
        off = int(parts[1], 0) if len(parts) > 1 else 0
        ln = int(parts[2], 0) if len(parts) > 2 else 4
    except Exception:
        return "формат: fpga read <node> <offset> [len]"
    raw = f.dma_read(node, off, ln)
    if not raw:
        return f"🔴 чтение {node}@0x{off:x} не удалось"
    u32 = f"0x{int.from_bytes(raw[:4], 'little'):08x}" if len(raw) >= 4 else "-"
    return (f"📟 {node}@0x{off:x} len={ln}\nhex: {raw.hex()}\n"
            f"le_u32: {u32}\nascii: {raw.decode('ascii', 'replace')}")


def _render_write(parts: list[str]) -> str:
    f = _fpga()
    if not f:
        return "❌ XilinxFPGA модуль недоступен"
    if len(parts) < 3:
        return "формат: fpga write <node> <offset> <hexbytes>  (node только user)"
    res = f.command("write", " ".join(parts))
    _obsidian_log("DMA WRITE", f"arg=`{' '.join(parts)}`\n```json\n{res}\n```")
    return "✍️ DMA WRITE\n" + res


def handle(text: str, core=None) -> Optional[str]:
    t = (text or "").lower().strip()
    if not any(tr in t for tr in TRIGGERS):
        return None
    parts = t.split()
    sub = parts[1] if len(parts) > 1 else "status"
    if sub in ("status", "stat"):
        return _render_status()
    if sub in ("bar", "bar_map", "map", "registers", "reg"):
        return _render_bar()
    if sub in ("read", "dma_read"):
        return _render_read(parts[2:])
    if sub in ("write", "dma_write"):
        return _render_write(parts[2:])
    if sub in ("monitor", "mon", "health"):
        return "📊 MONITOR\n```json\n" + json.dumps(snapshot(), ensure_ascii=False, indent=1) + "\n```"
    if sub in ("heal", "selfheal", "self_heal", "fix"):
        r = heal()
        return f"🩹 SELF-HEAL: {r['action']}\n```json\n" + json.dumps(r, ensure_ascii=False, indent=1) + "\n```"
    if sub in ("plan", "driver_plan"):
        f = _fpga()
        return f.driver_plan()[:3800] if f else "❌ модуль недоступен"
    if sub in ("probe", "dma"):
        f = _fpga()
        return ("```json\n" + json.dumps(f.dma_probe(), ensure_ascii=False, indent=1) + "\n```") if f else "❌"
    return ("fpga команды: status | bar | read <node> <off> [len] | "
            "write <node> <off> <hex> | monitor | heal | plan | dma")


def execute(text: str = "") -> str:
    """Точка входа SkillLoader."""
    r = handle(text or "fpga status")
    return r if r is not None else _render_status()
