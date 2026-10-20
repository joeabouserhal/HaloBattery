"""ATK V75X battery over its 2.4 GHz receiver.

Source: ATK HUB 3.2.29, line 1 of the Evision keyboard driver:
https://bpcdn.atkgear.com/hub-v3/production/3.2.29/static/keyboard-info.util-D8BvmqVT.js
Mr.getPowerInfo / sr send command 1A; xr.transferForResult uses report 04 and
matches a uint16 request id; Mr.init uses 31 payload bytes for wireless;
Ge.createBattery reads data[0] as percent and data[1] == 2 as charging.
The device table in demoMode.store-CYJllHvf.js binds ATK V75X 2.4G Dongle to
320F:5088, FF1C:0092. Other keyboards share these ids: match the product too.
No setup or configuration writes are needed. The request-id echo validates
responses independently; the battery query is the only command sent.
Confirmed on a V75X receiver: 04 01 6a 1a 00 00 00 00 3b 00 ... = 59%,
discharging. Charging parsing follows the driver and is not hardware-verified.
"""
from __future__ import annotations

import time
from typing import List, Optional, Tuple

try:
    import hid
except ImportError:  # pragma: no cover
    hid = None

from . import hidlist
from .base import DeviceStatus, Provider, hexdump, log

VID, PID = 0x320F, 0x5088
USAGE_PAGE, USAGE = 0xFF1C, 0x0092
PRODUCT = "ATK V75X 2.4G Dongle"
REPORT_ID, CMD_BATTERY = 0x04, 0x1A
TIMEOUT, READ_MS = 1.0, 100


def battery_reply(report, sequence: int) -> Optional[Tuple[int, bool]]:
    """Only accept the battery response to this request, including the report id."""
    if (len(report) < 10 or report[0] != REPORT_ID
            or report[1] != (sequence & 0xFF) or report[2] != (sequence >> 8)
            or report[3] != CMD_BATTERY or any(report[5:7])):
        return None
    level, state = report[8:10]
    if not 0 <= level <= 100 or state not in (0, 1, 2):
        return None
    return level, state == 2  # ATK's keyboard driver uses 2 for charging


class AtkKeyboardProvider(Provider):
    name = "atk_keyboard"

    def __init__(self):
        self._diag: List[str] = []
        self._sequence = 0

    def _read(self, path) -> Optional[Tuple[int, bool]]:
        self._sequence = self._sequence % 0xFFFF + 1
        sequence = self._sequence
        dev = hid.device()
        try:
            dev.open_path(path)
            # The battery query alone works; do not enter a configuration session.
            dev.write([REPORT_ID, sequence & 0xFF, sequence >> 8, CMD_BATTERY] + [0] * 28)
            deadline = time.monotonic() + TIMEOUT
            while time.monotonic() < deadline:
                report = dev.read(64, READ_MS)
                answer = battery_reply(report, sequence)
                if answer is not None:
                    self._diag.append(f"  battery: {hexdump(report, 10)} -> {answer[0]}%, charging={answer[1]}")
                    return answer
            self._diag.append("  no valid battery reply (asleep, off, or busy)")
        except (OSError, ValueError) as e:
            self._diag.append(f"  battery read: {e}")
        finally:
            try:
                dev.close()
            except Exception:
                pass
        return None

    def poll(self) -> List[DeviceStatus]:
        self._diag = []
        if hid is None:
            return []
        try:
            infos = hidlist.enumerate(VID)
        except Exception as e:  # pragma: no cover
            log.warning("hid.enumerate(atk_keyboard): %s", e)
            return []
        for d in infos:
            if (d.get("product_id") != PID
                    or (d.get("usage_page"), d.get("usage")) != (USAGE_PAGE, USAGE)
                    or (d.get("product_string") or "").strip().casefold() != PRODUCT.casefold()):
                continue
            self._diag.append(f"[ATK V75X] {VID:04x}:{PID:04x} 2.4 GHz")
            answer = self._read(d["path"])
            level, charging = answer if answer is not None else (None, False)
            return [DeviceStatus("atk_keyboard:320f:5088", "ATK V75X", level, charging,
                                 answer is not None, self.name, kind="keyboard",
                                 wake_on_input=answer is None)]
        return []

    def diagnostics(self) -> List[str]:
        return list(self._diag)
