"""
Xilinx/AMD FPGA PCIe bridge for ARGOS.

This module does not pretend that an FPGA can be used before a driver and
bitstream exist. It gives ARGOS a reliable first integration layer:
detect the PCIe endpoint, expose driver state, and describe the next driver
step (XDMA/QDMA/vendor driver).
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from typing import Any


@dataclass
class FpgaDevice:
    status: str = ""
    friendly_name: str = ""
    instance_id: str = ""
    problem: str = ""
    problem_code: str = ""
    problem_status: str = ""
    config_flags: str = ""
    service: str = ""
    class_guid: str = ""
    hardware_ids: list[str] | None = None
    compatible_ids: list[str] | None = None
    location: str = ""
    bus_number: str = ""
    current_link_speed: str = ""
    current_link_width: str = ""
    max_link_speed: str = ""
    max_link_width: str = ""
    base_class: str = ""
    sub_class: str = ""
    prog_if: str = ""

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "FpgaDevice":
        hwids = raw.get("HardwareIds") or []
        if isinstance(hwids, str):
            hwids = [hwids]
        compat_ids = raw.get("CompatibleIds") or []
        if isinstance(compat_ids, str):
            compat_ids = [compat_ids]
        return cls(
            status=str(raw.get("Status") or ""),
            friendly_name=str(raw.get("FriendlyName") or raw.get("Name") or "Xilinx FPGA"),
            instance_id=str(raw.get("InstanceId") or ""),
            problem=str(raw.get("Problem") or ""),
            problem_code=str(raw.get("ProblemCode") or ""),
            problem_status=str(raw.get("ProblemStatus") or ""),
            config_flags=str(raw.get("ConfigFlags") or ""),
            service=str(raw.get("Service") or ""),
            class_guid=str(raw.get("ClassGuid") or ""),
            hardware_ids=[str(x) for x in hwids],
            compatible_ids=[str(x) for x in compat_ids],
            location=str(raw.get("LocationInfo") or ""),
            bus_number=str(raw.get("BusNumber") or ""),
            current_link_speed=str(raw.get("CurrentLinkSpeed") or ""),
            current_link_width=str(raw.get("CurrentLinkWidth") or ""),
            max_link_speed=str(raw.get("MaxLinkSpeed") or ""),
            max_link_width=str(raw.get("MaxLinkWidth") or ""),
            base_class=str(raw.get("BaseClass") or ""),
            sub_class=str(raw.get("SubClass") or ""),
            prog_if=str(raw.get("ProgIf") or ""),
        )

    @property
    def driver_missing(self) -> bool:
        return self.problem_code == "28" or "FAILED_INSTALL" in self.problem

    @property
    def problem_status_hex(self) -> str:
        try:
            return f"0x{int(str(self.problem_status), 10):08X}"
        except Exception:
            return self.problem_status

    @property
    def subsystem(self) -> str:
        for hwid in self.hardware_ids or []:
            if "SUBSYS_" in hwid:
                return hwid.split("SUBSYS_", 1)[1].split("&", 1)[0]
        return ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "friendly_name": self.friendly_name,
            "instance_id": self.instance_id,
            "problem": self.problem,
            "problem_code": self.problem_code,
            "problem_status": self.problem_status,
            "problem_status_hex": self.problem_status_hex,
            "config_flags": self.config_flags,
            "service": self.service,
            "class_guid": self.class_guid,
            "driver_missing": self.driver_missing,
            "hardware_ids": self.hardware_ids or [],
            "compatible_ids": self.compatible_ids or [],
            "subsystem": self.subsystem,
            "location": self.location,
            "bus_number": self.bus_number,
            "current_link_speed": self.current_link_speed,
            "current_link_width": self.current_link_width,
            "max_link_speed": self.max_link_speed,
            "max_link_width": self.max_link_width,
            "base_class": self.base_class,
            "sub_class": self.sub_class,
            "prog_if": self.prog_if,
        }


# Дефолтные Device ID Xilinx XDMA (DMA/Bridge Subsystem for PCIe).
# Если устройство имеет один из них — это XDMA-дизайн, и нужен Xilinx XDMA-драйвер.
# Источник: INF официального драйвера github.com/Xilinx/dma_ip_drivers (XDMA/windows).
_XDMA_DEVICE_IDS = {
    "7011", "7012", "7014", "7021", "7022", "7024",
    "8011", "8012", "8014", "8018", "8021", "8022", "8024", "8028",
    "9011", "9012", "9014", "9018", "9021", "9022", "9024", "9028",
    "6828", "6830", "6928", "6930", "6A28", "6A30", "6D30",
}


def _dev_id_from_instance(instance_id: str) -> str:
    iid = (instance_id or "").upper()
    if "DEV_" in iid:
        return iid.split("DEV_", 1)[1].split("&", 1)[0]
    return ""


class XilinxFPGA:
    """Detect and report Xilinx FPGA PCIe endpoints."""

    def detect(self) -> dict[str, Any]:
        if os.name != "nt":
            return {
                "ok": False,
                "platform": os.name,
                "devices": [],
                "message": "Windows PnP detection is only available on Windows.",
            }

        ps = r"""
$ErrorActionPreference='SilentlyContinue'
[Console]::OutputEncoding=[System.Text.UTF8Encoding]::UTF8
$devs = Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -like 'PCI\VEN_10EE*' }
$out = @()
foreach ($d in $devs) {
  $props = Get-PnpDeviceProperty -InstanceId $d.InstanceId
  $map = @{}
  foreach ($p in $props) { $map[$p.KeyName] = $p.Data }
  $out += [pscustomobject]@{
    Status=$d.Status
    Class=$d.Class
    FriendlyName=$d.FriendlyName
    InstanceId=$d.InstanceId
    Problem=$d.Problem
    ProblemDescription=$d.ProblemDescription
    HardwareIds=$map['DEVPKEY_Device_HardwareIds']
    CompatibleIds=$map['DEVPKEY_Device_CompatibleIds']
    ConfigFlags=$map['DEVPKEY_Device_ConfigFlags']
    Service=$map['DEVPKEY_Device_Service']
    ClassGuid=$map['DEVPKEY_Device_ClassGuid']
    LocationInfo=$map['DEVPKEY_Device_LocationInfo']
    BusNumber=$map['DEVPKEY_Device_BusNumber']
    ProblemCode=$map['DEVPKEY_Device_ProblemCode']
    ProblemStatus=$map['DEVPKEY_Device_ProblemStatus']
    CurrentLinkSpeed=$map['DEVPKEY_PciDevice_CurrentLinkSpeed']
    CurrentLinkWidth=$map['DEVPKEY_PciDevice_CurrentLinkWidth']
    MaxLinkSpeed=$map['DEVPKEY_PciDevice_MaxLinkSpeed']
    MaxLinkWidth=$map['DEVPKEY_PciDevice_MaxLinkWidth']
    BaseClass=$map['DEVPKEY_PciDevice_BaseClass']
    SubClass=$map['DEVPKEY_PciDevice_SubClass']
    ProgIf=$map['DEVPKEY_PciDevice_ProgIf']
  }
}
$out | ConvertTo-Json -Depth 6
"""
        try:
            completed = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=25,
            )
        except Exception as exc:
            return {"ok": False, "devices": [], "error": str(exc)}

        if completed.returncode != 0:
            return {"ok": False, "devices": [], "error": completed.stderr.strip()[:800]}

        text = completed.stdout.strip()
        if not text:
            return {"ok": False, "devices": []}

        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            return {"ok": False, "devices": [], "error": f"PnP JSON parse failed: {exc}"}

        raw_devices = payload if isinstance(payload, list) else [payload]
        devices = [FpgaDevice.from_raw(item).as_dict() for item in raw_devices if isinstance(item, dict)]
        return {
            "ok": bool(devices),
            "devices": devices,
            "driver_missing": any(device.get("driver_missing") for device in devices),
        }

    def status(self) -> str:
        info = self.detect()
        lines = ["XILINX FPGA STATUS"]
        lines.append("=" * 32)
        if not info.get("ok"):
            lines.append("❌ Xilinx PCIe FPGA не обнаружена.")
            if info.get("error"):
                lines.append(f"error: {info['error']}")
            return "\n".join(lines)

        for idx, device in enumerate(info.get("devices", []), 1):
            state = "driver missing" if device.get("driver_missing") else "detected"
            lines.append(f"#{idx}: {device.get('friendly_name') or 'Xilinx FPGA'} — {state}")
            lines.append(f"  InstanceId: {device.get('instance_id')}")
            lines.append(f"  SUBSYS: {device.get('subsystem') or 'unknown'}")
            lines.append(f"  Location: {device.get('location') or 'unknown'}")
            lines.append(
                "  PCIe: "
                f"speed {device.get('current_link_speed')}/{device.get('max_link_speed')}, "
                f"width x{device.get('current_link_width')}/x{device.get('max_link_width')}"
            )
            lines.append(
                f"  Class: base={device.get('base_class')} "
                f"sub={device.get('sub_class')} prog_if={device.get('prog_if')}"
            )
            if device.get("problem_status_hex"):
                lines.append(f"  ProblemStatus: {device.get('problem_status_hex')}")
            if device.get("config_flags"):
                lines.append(f"  ConfigFlags: {device.get('config_flags')}")
            lines.append(f"  Service: {device.get('service') or 'none'}")
            lines.append(f"  ClassGuid: {device.get('class_guid') or 'none'}")
            if device.get("driver_missing"):
                lines.append("  Windows Code 28: драйвер не установлен.")
        lines.append("")
        lines.append("ARGOS mode: inventory/diagnostics only until XDMA/QDMA/vendor driver + bitstream are installed.")
        return "\n".join(lines)

    def is_xdma(self) -> bool:
        """True, если Device ID соответствует дефолтному XDMA (нужен XDMA-драйвер)."""
        info = self.detect()
        for d in info.get("devices", []):
            if _dev_id_from_instance(d.get("instance_id", "")) in _XDMA_DEVICE_IDS:
                return True
        return False

    def driver_plan(self) -> str:
        xdma = self.is_xdma()
        lines = ["XILINX FPGA DRIVER PLAN", "=" * 32]
        if xdma:
            lines += [
                "ОПОЗНАНО: Device ID = дефолтный Xilinx XDMA (DMA/Bridge for PCIe).",
                "→ Нужен официальный Xilinx XDMA-драйвер. Не driver-pack, не DriverIdentifier.",
                "",
                "Источник (официальный): github.com/Xilinx/dma_ip_drivers → XDMA/windows",
                "INF поддерживает VEN_10EE&DEV_7022 (проверено).",
                "",
                "Установка (нужны ADMIN + перезагрузка — XDMA-драйвер test-signed):",
                "  1. bcdedit /set testsigning on   (от админа)",
                "  2. Перезагрузка (в углу появится 'Test Mode').",
                "  3. pnputil /add-driver xdma.inf /install   ИЛИ через Диспетчер устройств →",
                "     'Сопроцессор' → Обновить драйвер → указать папку с xdma.inf",
                "  4. После — устройство станет 'Xilinx Drivers/XDMA', Code 28 уйдёт.",
                "",
                "ВАЖНО: XDMA-драйвер даёт только DMA/доступ к регистрам (BAR).",
                "Что FPGA реально считает — зависит от ПРОШИТОГО bitstream. Без знания",
                "bitstream это канал данных, а не готовый ИИ-ускоритель.",
                "PCIe линк узкий (x1/x2) → пропускная способность ~0.5-1 ГБ/с.",
            ]
        else:
            lines += [
                "1. Не ставить случайные driver-pack.",
                "2. Найти драйвер под текущий bitstream: XDMA, QDMA или vendor-specific.",
                "3. Для Windows проверить INF на поддержку VEN_10EE&DEV_xxxx.",
                "4. После драйвера появятся device nodes/API; ARGOS сможет читать регистры/DMA.",
                "5. До этого FPGA — обнаруженное PCIe-устройство, но не ускоритель.",
            ]
        return "\n".join(lines)

    # ─────────────────── AMD/Xilinx Vivado/Vitis toolchain ───────────────────
    # Корни, где обычно ставится Unified SDI (Vivado + Vitis). C: мал → ищем шире.
    _XILINX_ROOTS = [
        r"J:\Xilinx", r"J:\2025.2", r"D:\Xilinx", r"E:\Xilinx", r"C:\Xilinx",
        r"C:\Program Files\Xilinx", r"F:\Xilinx", r"J:\AMD", r"C:\AMD",
    ]

    def _find_tool(self, exe_names: tuple[str, ...]) -> str:
        """Ищет vivado.bat / vitis.bat / v++.bat в типичных install-корнях."""
        import glob
        for root in self._XILINX_ROOTS:
            if not os.path.isdir(root):
                continue
            for exe in exe_names:
                # <root>\Vivado\<ver>\bin\vivado.bat  и т.п.
                for hit in glob.glob(os.path.join(root, "**", "bin", exe), recursive=True):
                    return hit
        return ""

    def toolchain(self) -> dict:
        """Что из AMD-тулчейна установлено (Vivado/Vitis/Vitis HLS/v++)."""
        vivado = self._find_tool(("vivado.bat", "vivado"))
        vitis = self._find_tool(("vitis.bat", "vitis"))
        vpp = self._find_tool(("v++.bat", "v++"))
        hls = self._find_tool(("vitis_hls.bat", "vitis_hls", "vivado_hls.bat"))
        def _ver(p):
            # .../Vivado/2025.2/bin/vivado.bat -> 2025.2
            parts = p.replace("/", "\\").split("\\")
            return next((parts[i - 1] for i, s in enumerate(parts) if s.lower() == "bin"), "") if p else ""
        return {
            "vivado": vivado, "vivado_version": _ver(vivado),
            "vitis": vitis, "vpp_xrt": vpp, "vitis_hls": hls,
            "ready": bool(vivado),
        }

    def toolchain_status(self) -> str:
        tc = self.toolchain()
        lines = ["AMD/XILINX TOOLCHAIN", "=" * 32]
        if not tc["ready"]:
            lines.append("⏳ Vivado не найден — установка ещё идёт или не на C:/D:/E:/J:\\Xilinx.")
            lines.append("   (Unified SDI 2025.2 ~100-200 ГБ; ставить на J:\\Xilinx — там место.)")
            return "\n".join(lines)
        lines.append(f"✅ Vivado {tc['vivado_version']}: {tc['vivado']}")
        for k, label in (("vitis", "Vitis"), ("vpp_xrt", "v++/XRT"), ("vitis_hls", "Vitis HLS")):
            lines.append(f"  {'✅' if tc[k] else '—'} {label}: {tc[k] or 'нет'}")
        lines.append("")
        lines.append("ARGOS может: программировать FPGA (program_bitstream), собирать bitstream,")
        lines.append("компилировать Vitis-ядра (v++), читать DMA после XDMA-драйвера.")
        return "\n".join(lines)

    def program_bitstream(self, bit_path: str, dry_run: bool = True) -> str:
        """Программирует FPGA через Vivado Hardware Manager (JTAG). dry_run по умолчанию."""
        tc = self.toolchain()
        if not tc["ready"]:
            return "❌ Vivado не установлен — программировать нечем."
        if not os.path.isfile(bit_path):
            return f"❌ bitstream не найден: {bit_path}"
        tcl = (
            "open_hw_manager\n"
            "connect_hw_server\n"
            "open_hw_target\n"
            "set dev [lindex [get_hw_devices] 0]\n"
            "current_hw_device $dev\n"
            f"set_property PROGRAM.FILE {{{bit_path}}} $dev\n"
            "program_hw_devices $dev\n"
            "refresh_hw_device $dev\n"
            "close_hw_manager\n"
        )
        tcl_path = os.path.join(os.getenv("TEMP", "."), "argos_fpga_program.tcl")
        try:
            with open(tcl_path, "w", encoding="utf-8") as f:
                f.write(tcl)
        except Exception as e:
            return f"❌ не записать TCL: {e}"
        cmd = f'"{tc["vivado"]}" -mode batch -source "{tcl_path}"'
        if dry_run:
            return f"DRY-RUN. Команда программирования FPGA:\n  {cmd}\nЗапусти program_bitstream(path, dry_run=False) для реального прошива."
        try:
            import subprocess
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=300)
            out = (r.stdout or "")[-600:] + (r.stderr or "")[-300:]
            ok = "End of startup status: HIGH" in out or r.returncode == 0
            return ("✅ FPGA запрограммирована." if ok else "⚠️ возможно ошибка.") + "\n" + out[-500:]
        except Exception as e:
            return f"❌ ошибка программирования: {e}"

    # ── XDMA DMA-доступ (Windows, ctypes, без pywin32) ────────────────────
    # GUID_DEVINTERFACE_XDMA из xdma_public.h (D:\xdma_driver_win-2020.5).
    XDMA_INTERFACE_GUID = "{74c7e4a9-6d5d-4a70-bc0d-20691dff9e9d}"

    def _xdma_base_path(self) -> str | None:
        """Базовый DevicePath XDMA через cfgmgr32 CM_Get_Device_Interface_ListW.
        Надёжнее SetupAPI в ctypes (нет усечения HDEVINFO на x64).
        None если интерфейс не зарегистрирован (= драйвер не привязан)."""
        if os.name != "nt":
            return None
        import ctypes as C
        from ctypes import wintypes as W

        class GUID(C.Structure):
            _fields_ = [("Data1", W.DWORD), ("Data2", W.WORD),
                        ("Data3", W.WORD), ("Data4", C.c_ubyte * 8)]

        try:
            cm = C.windll.cfgmgr32
            ole = C.windll.ole32
            g = GUID()
            if ole.CLSIDFromString(self.XDMA_INTERFACE_GUID, C.byref(g)) != 0:
                return None
            CM_PRESENT = 0  # CM_GET_DEVICE_INTERFACE_LIST_PRESENT
            size = W.ULONG(0)
            if cm.CM_Get_Device_Interface_List_SizeW(
                    C.byref(size), C.byref(g), None, CM_PRESENT) != 0:
                return None
            if size.value <= 1:  # пустой MULTI_SZ — интерфейсов нет
                return None
            buf = (C.c_wchar * size.value)()
            if cm.CM_Get_Device_Interface_ListW(
                    C.byref(g), None, buf, size.value, CM_PRESENT) != 0:
                return None
            first = C.wstring_at(buf)  # первый элемент MULTI_SZ
            return first or None
        except Exception:
            return None

    def dma_read(self, node: str = "control", offset: int = 0,
                 length: int = 4) -> bytes | None:
        """Чтение из XDMA узла. node: control|user|c2h_0|... None если недоступно."""
        if os.name != "nt":
            return None
        base = self._xdma_base_path()
        if not base:
            return None
        import ctypes as C
        from ctypes import wintypes as W
        path = base + "\\" + node
        GENERIC_READ, GENERIC_WRITE, OPEN_EXISTING = 0x80000000, 0x40000000, 3
        k = C.windll.kernel32
        # restype обязателен: без него HANDLE усекается до c_int на x64
        k.CreateFileW.restype = C.c_void_p
        k.SetFilePointerEx.argtypes = [C.c_void_p, C.c_longlong,
                                       C.c_void_p, W.DWORD]
        k.ReadFile.argtypes = [C.c_void_p, C.c_void_p, W.DWORD,
                               C.POINTER(W.DWORD), C.c_void_p]
        k.CloseHandle.argtypes = [C.c_void_p]
        INVALID = C.c_void_p(-1).value
        h = k.CreateFileW(path, GENERIC_READ | GENERIC_WRITE, 0, None,
                          OPEN_EXISTING, 0, None)
        if h is None or h == INVALID:
            return None
        try:
            k.SetFilePointerEx(h, C.c_longlong(offset), None, 0)  # FILE_BEGIN
            buf = C.create_string_buffer(length)
            got = W.DWORD(0)
            if not k.ReadFile(h, buf, length, C.byref(got), None):
                return None
            return buf.raw[:got.value]
        finally:
            k.CloseHandle(h)

    def dma_write(self, node: str, offset: int, data: bytes) -> dict[str, Any]:
        """Запись в XDMA узел (PIO в BAR через WriteFile) + read-back для верификации.
        ВНИМАНИЕ: пишет в реальные регистры FPGA. По умолчанию разрешён только
        'user' (безопасный пользовательский BAR); control/h2c/c2h блокируются,
        чтобы случайно не переинициализировать DMA-движок."""
        res: dict[str, Any] = {"ok": False, "node": node,
                               "offset": f"0x{offset:x}", "written": len(data),
                               "readback_hex": None, "verified": None, "note": ""}
        if os.name != "nt":
            res["note"] = "не Windows"
            return res
        # guardrail: запись только в user BAR (контрольные регистры не трогаем)
        if node.lower() not in ("user",):
            res["note"] = (f"запись в '{node}' заблокирована — разрешён только 'user' "
                           "(control/h2c/c2h управляют DMA-движком, риск)")
            return res
        base = self._xdma_base_path()
        if not base:
            res["note"] = "интерфейс XDMA не зарегистрирован"
            return res
        import ctypes as C
        from ctypes import wintypes as W
        path = base + "\\" + node
        GENERIC_READ, GENERIC_WRITE, OPEN_EXISTING = 0x80000000, 0x40000000, 3
        k = C.windll.kernel32
        k.CreateFileW.restype = C.c_void_p
        k.SetFilePointerEx.argtypes = [C.c_void_p, C.c_longlong, C.c_void_p, W.DWORD]
        k.WriteFile.argtypes = [C.c_void_p, C.c_void_p, W.DWORD,
                                C.POINTER(W.DWORD), C.c_void_p]
        k.CloseHandle.argtypes = [C.c_void_p]
        INVALID = C.c_void_p(-1).value
        h = k.CreateFileW(path, GENERIC_READ | GENERIC_WRITE, 0, None,
                          OPEN_EXISTING, 0, None)
        if h is None or h == INVALID:
            res["note"] = "CreateFile не удалось"
            return res
        try:
            k.SetFilePointerEx(h, C.c_longlong(offset), None, 0)
            wbuf = C.create_string_buffer(bytes(data), len(data))
            wrote = W.DWORD(0)
            if not k.WriteFile(h, wbuf, len(data), C.byref(wrote), None):
                res["note"] = "WriteFile вернул FALSE"
                return res
            res["written"] = wrote.value
        finally:
            k.CloseHandle(h)
        rb = self.dma_read(node, offset, len(data))
        if rb is not None:
            res["readback_hex"] = rb.hex()
            res["verified"] = (rb == bytes(data))
        res["ok"] = True
        res["note"] = ("записано + read-back "
                       + ("совпал ✓" if res["verified"] else "НЕ совпал (регистр RO/side-effect?)"))
        return res

    def dma_probe(self) -> dict[str, Any]:
        """Проверка реального DMA-доступа. Читает \\control offset 0 →
        XDMA Identifier (0x1fc0xxxx) — надёжно независимо от bitstream."""
        result: dict[str, Any] = {
            "interface_registered": False,
            "device_path": None,
            "control_id_hex": None,
            "xdma_signature_ok": False,
            "note": "",
        }
        base = self._xdma_base_path()
        if not base:
            result["note"] = ("XDMA interface не зарегистрирован — драйвер не "
                              "привязан к устройству (Service пуст / служба XDMA "
                              "отсутствует). Сначала довести установку oem57.inf "
                              "(pnputil /install + /restart-device от админа).")
            return result
        result["interface_registered"] = True
        result["device_path"] = base
        raw = self.dma_read("control", 0x0, 4)
        if raw and len(raw) == 4:
            val = int.from_bytes(raw, "little")
            result["control_id_hex"] = f"0x{val:08x}"
            # XDMA H2C/C2H channel identifier старшие байты 0x1fc0
            result["xdma_signature_ok"] = (val >> 16) == 0x1fc0
            result["note"] = ("DMA control доступен; сигнатура XDMA "
                              + ("найдена ✓" if result["xdma_signature_ok"]
                                 else "НЕ совпала (проверить bitstream/IP)"))
        else:
            result["note"] = ("Interface есть, но чтение \\control не удалось "
                              "(GetLastError) — устройство/движок не отвечает.")
        return result

    # XDMA control-BAR субмодули (XDMA PG195): идентификатор в offset 0x0 каждого.
    _XDMA_SUBMODULES = [
        ("H2C0",   0x0000), ("C2H0",   0x1000), ("IRQ",    0x2000),
        ("Config", 0x3000), ("H2C_SGDMA", 0x4000), ("C2H_SGDMA", 0x5000),
        ("SGDMA_common", 0x6000), ("MSIX", 0x8000),
    ]

    def bar_map(self) -> dict[str, Any]:
        """Живая карта control-BAR: ID каждого XDMA-субмодуля + user-сигнатура.
        Реальное чтение через драйвер, без моков."""
        out: dict[str, Any] = {"available": False, "control": {}, "user": None,
                               "note": ""}
        base = self._xdma_base_path()
        if not base:
            out["note"] = "XDMA interface не зарегистрирован (драйвер не привязан)"
            return out
        out["available"] = True
        for name, off in self._XDMA_SUBMODULES:
            r = self.dma_read("control", off, 4)
            if r and len(r) == 4:
                val = int.from_bytes(r, "little")
                out["control"][name] = {
                    "offset": f"0x{off:04x}", "id": f"0x{val:08x}",
                    "subsystem": f"0x{val >> 12:04x}",
                    "version": val & 0xff,
                }
        u = self.dma_read("user", 0x0, 16)
        if u:
            out["user"] = {
                "hex": u.hex(),
                "ascii": u[:8].decode("ascii", "replace"),
            }
        ids = [v["id"] for v in out["control"].values()]
        out["note"] = (f"{len(ids)} субмодулей прочитано; "
                       + ("XDMA сигнатура 0x1fc0 ✓"
                          if any(i.startswith("0x1fc") for i in ids)
                          else "сигнатура не XDMA"))
        return out

    def command(self, action: str = "status", arg: str = "") -> str:
        action = (action or "status").strip().lower()
        if action == "status":
            return self.status()
        if action in {"detect", "json"}:
            return json.dumps(self.detect(), ensure_ascii=False, indent=2)
        if action in {"plan", "driver_plan"}:
            return self.driver_plan()
        if action in {"toolchain", "vivado", "vitis"}:
            return self.toolchain_status()
        if action in {"program", "program_bitstream"}:
            return self.program_bitstream(arg, dry_run=True)
        if action in {"dma_test", "dma_probe", "dma"}:
            return json.dumps(self.dma_probe(), ensure_ascii=False, indent=2)
        if action in {"bar_map", "barmap", "bar", "map"}:
            return json.dumps(self.bar_map(), ensure_ascii=False, indent=2)
        if action in {"dma_write", "write"}:
            # arg формат: "node offset hexbytes", напр. "user 0x20 deadbeef"
            parts = (arg or "").split()
            if len(parts) < 3:
                return "формат: write <node> <offset> <hexbytes>  (node только user)"
            node = parts[0]
            try:
                off = int(parts[1], 0)
                data = bytes.fromhex(parts[2])
            except Exception as e:
                return f"ошибка разбора: {e}"
            return json.dumps(self.dma_write(node, off, data),
                              ensure_ascii=False, indent=2)
        if action in {"dma_read", "read"}:
            # arg формат: "node offset [len]", напр. "user 0 16" или "control 0x1000 4"
            parts = (arg or "control 0 4").split()
            node = parts[0] if parts else "control"
            off = int(parts[1], 0) if len(parts) > 1 else 0
            ln = int(parts[2], 0) if len(parts) > 2 else 4
            raw = self.dma_read(node, off, ln)
            return json.dumps({
                "node": node, "offset": f"0x{off:x}", "length": ln,
                "hex": raw.hex() if raw else None,
                "le_u32": (f"0x{int.from_bytes(raw[:4],'little'):08x}"
                           if raw and len(raw) >= 4 else None),
                "ascii": (raw.decode("ascii", "replace") if raw else None),
            }, ensure_ascii=False, indent=2)
        return f"Unknown xilinx_fpga action: {action}"
