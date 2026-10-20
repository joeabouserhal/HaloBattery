"""Lofree Hyzen and ATK V75X keyboards on their Evision 2.4 GHz dongles.

Source: Lofree's own web driver (hyzen.lofree.tech). Its code is obfuscated; the
exchange below comes from its device list and from its battery routine, with the
strings decoded by the page's own decoder functions.

Device list: vendor 0x388D, collection usage page 0xFF1C, usage 0x92:
    0x0024  Hyzen on the USB cable     (connection type 1, device id 101)
    0x0025  Hyzen on the 2.4 GHz dongle (connection type 2, device id 101)
    0x0029  a second model on the cable (device id 102)

Every command is a small transaction on HID report 0x04. After the report id, a
frame is 31 bytes on the dongle (63 on the cable):

    host   00 00 01                       start
    kbd    .. .. 01 ...                   byte 2 = 01: start acknowledged
    host   00 00 <cmd> 00 00 00 00        the command, no data, offset 0
    kbd    .. .. <cmd> .. 00 00 .. <d0>   byte 2 = cmd (or 00), bytes 4-5 = offset 0,
                                          the answer starts at byte 7
    host   00 00 02                       end
    kbd    .. .. 02 ...                   byte 2 = 02: end acknowledged

The web driver reads the battery only over the dongle, and not when the same
keyboard is also on its cable:
    command 0xAA  "is the keyboard online": d0 = 0 means offline
    command 0x1A  battery: d0 = the level in %
The Lofree web driver shows no charging state, so none is claimed for Lofree.

ATK source: ATK HUB 3.2.29, Evision keyboard driver, line 1 of
https://bpcdn.atkgear.com/hub-v3/production/3.2.29/static/keyboard-info.util-D8BvmqVT.js
Mr.getPowerInfo / sr send command 1A; xr.transferForResult uses report 04 and
matches a uint16 request id; Mr.init uses 31 payload bytes for wireless;
Ge.createBattery reads data[0] as percent and data[1] == 2 as charging.
The device table in demoMode.store-CYJllHvf.js binds ATK V75X 2.4G Dongle to
320F:5088, FF1C:0092. Other keyboards share these ids: match the product too.
The V75X needs only the battery query, without Lofree's start/end handshake.
Confirmed on a V75X receiver: 04 01 6a 1a 00 00 00 00 3b 00 ... = 59%,
discharging. Charging parsing follows the driver and is not hardware-verified.
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional, Tuple

try:
    import hid
except ImportError:              # pragma: no cover
    hid = None

from . import hidlist
from .base import DeviceStatus, Provider, hexdump, log

LOFREE_VID = 0x388D
ATK_VID, ATK_PID = 0x320F, 0x5088
ATK_PRODUCT = "ATK V75X 2.4G Dongle"
ATK_TIMEOUT = 1.0
USAGE_PAGE, USAGE = 0xFF1C, 0x0092

DONGLES = {0x0025: "Lofree Hyzen"}           # the pids that are read (2.4 GHz)
WIRED_OF = {0x0025: 0x0024}                  # the same keyboard on its cable

REPORT_ID = 0x04
FRAME = 31                                   # bytes after the report id, on the dongle
START, END = 0x01, 0x02
CMD_ONLINE, CMD_BATTERY = 0xAA, 0x1A
DATA_AT = 7                                  # the answer starts here (after the report id)
TIMEOUT = 3.0                                # s, the web driver's minimum timeout
READ_MS = 100


def frame(b2: int) -> List[int]:
    """Report 0x04 with byte 2 set: 00 00 <b2> 00 ..., 31 bytes after the id."""
    return [REPORT_ID, 0x00, 0x00, b2] + [0x00] * (FRAME - 3)


def payload(r) -> Optional[List[int]]:
    """The bytes after the report id of an input report 0x04, or None."""
    if not r or len(r) < 2 or r[0] != REPORT_ID:
        return None
    return list(r[1:])


def atk_battery_reply(report, sequence: int) -> Optional[Tuple[int, bool]]:
    """Only accept the battery response to this request, including the report id."""
    if (len(report) < 10 or report[0] != REPORT_ID
            or report[1] != (sequence & 0xFF) or report[2] != (sequence >> 8)
            or report[3] != CMD_BATTERY or any(report[5:7])):
        return None
    level, state = report[8:10]
    if not 0 <= level <= 100 or state not in (0, 1, 2):
        return None
    return level, state == 2  # ATK's keyboard driver uses 2 for charging


class LofreeProvider(Provider):
    name = "lofree"

    def __init__(self):
        self._diag: List[str] = []
        self._atk_sequence = 0

    def _wait(self, dev, want, deadline: float) -> Optional[List[int]]:
        """Read reports 0x04 until `want(payload)` is true, or until the deadline."""
        while time.time() < deadline:
            try:
                r = dev.read(64, READ_MS)
            except (OSError, ValueError) as e:
                self._diag.append(f"    read: {e}")
                return None
            p = payload(r)
            if p is not None and want(p):
                return p
        return None

    def _command(self, dev, cmd: int) -> Optional[int]:
        """One transaction; the first answer byte, or None."""
        deadline = time.time() + TIMEOUT
        dev.write(frame(START))
        if self._wait(dev, lambda p: len(p) >= 3 and p[2] == START, deadline) is None:
            self._diag.append(f"    cmd {cmd:02x}: no start acknowledgement")
            return None
        dev.write([REPORT_ID, 0x00, 0x00, cmd, 0x00, 0x00, 0x00, 0x00] + [0x00] * (FRAME - 7))
        p = self._wait(dev, lambda p: (len(p) > DATA_AT and p[2] in (cmd, 0x00)
                                       and p[4] == 0x00 and p[5] == 0x00), deadline)
        # close the transaction in any case, as the web driver does
        dev.write(frame(END))
        self._wait(dev, lambda p: len(p) >= 3 and p[2] == END, min(deadline, time.time() + 0.5))
        if p is None:
            self._diag.append(f"    cmd {cmd:02x}: no answer")
            return None
        self._diag.append(f"    cmd {cmd:02x}: {hexdump(p, 10)}")
        return p[DATA_AT]

    def _read(self, path) -> Optional[int]:
        dev = hid.device()
        try:
            dev.open_path(path)
        except (OSError, IOError) as e:
            self._diag.append(f"    open: {e}")
            return None
        try:
            try:
                online = self._command(dev, CMD_ONLINE)
                if online is None:
                    return None
                if online == 0:
                    self._diag.append("    keyboard offline (off or asleep)")
                    return None
                level = self._command(dev, CMD_BATTERY)
            except (OSError, IOError, ValueError) as e:
                self._diag.append(f"    write: {e}")
                return None
            if level is None or not 0 <= level <= 100:
                if level is not None:
                    self._diag.append(f"    level {level} out of range, not shown")
                return None
            return level
        finally:
            try:
                dev.close()
            except Exception:
                pass

    def _read_atk(self, path) -> Optional[Tuple[int, bool]]:
        self._atk_sequence = self._atk_sequence % 0xFFFF + 1
        sequence = self._atk_sequence
        dev = hid.device()
        try:
            dev.open_path(path)
            # The battery query alone works; do not enter a configuration session.
            dev.write([REPORT_ID, sequence & 0xFF, sequence >> 8, CMD_BATTERY] + [0] * (FRAME - 3))
            deadline = time.monotonic() + ATK_TIMEOUT
            while time.monotonic() < deadline:
                report = dev.read(64, READ_MS)
                answer = atk_battery_reply(report, sequence)
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

    def _poll_atk(self) -> List[DeviceStatus]:
        try:
            infos = hidlist.enumerate(ATK_VID)
        except Exception as e:  # pragma: no cover
            log.warning("hid.enumerate(atk_keyboard): %s", e)
            return []
        for d in infos:
            if (d.get("product_id") != ATK_PID
                    or (d.get("usage_page"), d.get("usage")) != (USAGE_PAGE, USAGE)
                    or (d.get("product_string") or "").strip().casefold() != ATK_PRODUCT.casefold()):
                continue
            self._diag.append(f"[ATK V75X] {ATK_VID:04x}:{ATK_PID:04x} 2.4 GHz")
            answer = self._read_atk(d["path"])
            level, charging = answer if answer is not None else (None, False)
            return [DeviceStatus("atk_keyboard:320f:5088", "ATK V75X", level, charging,
                                 answer is not None, self.name, kind="keyboard",
                                 wake_on_input=answer is None)]
        return []

    def poll(self) -> List[DeviceStatus]:
        self._diag = []
        if hid is None:
            return []
        try:
            infos = hidlist.enumerate(LOFREE_VID)
        except Exception as e:  # pragma: no cover
            log.warning("hid.enumerate(lofree): %s", e)
            return []
        present = {d["product_id"] for d in infos}
        out: List[DeviceStatus] = []
        for d in infos:
            pid = d["product_id"]
            if pid not in DONGLES or (d.get("usage_page"), d.get("usage")) != (USAGE_PAGE, USAGE):
                continue
            product = (d.get("product_string") or "").split("@")[0].strip()
            name = f"Lofree {product}" if product else DONGLES[pid]
            self._diag.append(f"[Lofree] {name} pid={pid:04x}")
            if WIRED_OF.get(pid) in present:
                self._diag.append("  the keyboard is on its cable: the dongle is not read")
                continue
            level = self._read(d["path"])
            if level is not None:
                self._diag.append(f"  -> {level}%")
                out.append(DeviceStatus(f"lofree:{pid:04x}", name, level, False, True, "lofree",
                                        kind="keyboard"))
        return out + self._poll_atk()

    def diagnostics(self) -> List[str]:
        return list(self._diag)
