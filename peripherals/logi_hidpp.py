#!/usr/bin/env python3
"""Logitech gaming mice over HID++ 2.0 - DPI speeds, polling rate, button
assignments and macros, saved in the mouse's own memory (its onboard profile
and macro sectors), so they work with nothing running and on any PC.

Run by RGB Studio as `logi_hidpp.py serve` (one JSON command per line).
Protocol facts are from the public HID++ 2.0 documentation as used by Solaar,
libratbag and lowtech (feature 0x8100 onboard profiles, 0x2201 DPI, 0x8060
report rate); the code is our own.

Safety for every save:
  1. read the whole profile and check its CRC (refuse if it doesn't match -
     an unknown layout),
  2. back the original up to a file,
  3. change only the DPI/polling/button bytes, recompute the CRC, write
     (macros go to their own sectors first; a sector is only used if it's
     blank, ours, or already this profile's macro),
  4. read it back and compare,
  5. tell the mouse to reload the profile.
"""
import json
import os
import struct
import sys
import time

_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "peripherals.log")


def flog(msg):
    try:
        if os.path.exists(_LOG) and os.path.getsize(_LOG) > 512 * 1024:
            os.replace(_LOG, _LOG + ".old")
        with open(_LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%m-%d %H:%M:%S ") + msg + "\n")
    except Exception:
        pass


def _load_hid():
    try:
        import hid
        return hid
    except Exception:
        pass
    import subprocess
    import site
    import importlib
    subprocess.run([sys.executable, "-m", "pip", "install", "--user", "--quiet", "hidapi"],
                   capture_output=True, timeout=180)
    try:
        sys.path.append(site.getusersitepackages())
    except Exception:
        pass
    importlib.invalidate_caches()
    import hid
    return hid


LOGI_VID = 0x046D
SWID = 0x0B
F_ROOT, F_FEATURESET, F_NAME = 0x0000, 0x0001, 0x0005
F_DPI, F_RATE, F_ONBOARD = 0x2201, 0x8060, 0x8100


class HidppError(Exception):
    pass


def crc16(data, crc=0xFFFF):
    """CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF) - the profile checksum."""
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


class Mouse:
    def __init__(self, hidmod, path, idx, pid):
        self.h = hidmod.device()
        self.h.open_path(path)
        self.idx = idx
        self.pid = pid
        self.feat = {}

    def close(self):
        try:
            self.h.close()
        except Exception:
            pass

    def call(self, index, fn, params=b"", timeout=1.0):
        msg = bytes([0x11, self.idx, index, (fn << 4) | SWID]) + bytes(params)
        self.h.write(msg + bytes(20 - len(msg)))
        end = time.time() + timeout
        while time.time() < end:
            r = self.h.read(64, 100)
            if not r:
                continue
            r = bytes(r)
            if r[0] not in (0x10, 0x11) or r[1] != self.idx:
                continue
            if r[2] == 0xFF and r[3] == index:
                raise HidppError(f"error {r[5]:#x} (feature {index} fn {fn})")
            if r[2] == 0x8F:
                raise HidppError(f"device unreachable ({r[4]:#x})")
            if r[2] == index and (r[3] & 0x0F) == SWID and (r[3] >> 4) == fn:
                return r[4:]
        raise HidppError("no reply (mouse asleep?)")

    def f(self, fid):
        if fid not in self.feat:
            r = self.call(0, 0, bytes([fid >> 8, fid & 255]))
            self.feat[fid] = r[0] or None
        return self.feat[fid]

    # ---- reads ----------------------------------------------------------
    def name(self):
        i = self.f(F_NAME)
        if not i:
            return "Logitech mouse"
        n = self.call(i, 0)[0]
        out = b""
        while len(out) < n:
            out += self.call(i, 1, bytes([len(out)]))
        return out[:n].decode(errors="replace").strip()

    def dpi_range(self):
        i = self.f(F_DPI)
        if not i:
            return None
        raw = self.call(i, 1, b"\x00")[1:]
        vals = [(raw[k] << 8) | raw[k + 1] for k in range(0, len(raw) - 1, 2)]
        vals = vals[: vals.index(0)] if 0 in vals else vals
        lo, hi, step, plain = None, None, 1, []
        for k, v in enumerate(vals):
            if v >> 13 == 0x7:                      # 0xE0xx = "step" between neighbours
                step = v & 0x1FFF
                lo = vals[k - 1] if k else lo
                hi = vals[k + 1] if k + 1 < len(vals) else hi
            else:
                plain.append(v)
        if lo is None:
            lo, hi = (min(plain), max(plain)) if plain else (100, 25600)
        return {"min": lo, "max": hi, "step": step}

    def rates(self):
        i = self.f(F_RATE)
        if not i:
            return [1000]
        mask = self.call(i, 0)[0]
        return [1000 // (b + 1) for b in range(8) if mask & (1 << b)]

    def onboard(self):
        i = self.f(F_ONBOARD)
        if not i:
            raise HidppError("this mouse has no onboard memory")
        d = self.call(i, 0)
        desc = {"memory": d[0], "format": d[1], "macro": d[2], "profiles": d[3], "buttons": d[5],
                "sectors": d[6], "size": (d[7] << 8) | d[8]}
        mode = self.call(i, 2)[0]
        cur = self.call(i, 4)
        desc["mode"] = mode
        desc["current"] = (cur[0] << 8) | cur[1]
        return i, desc

    def read_sector(self, i, sector, size):
        data = bytearray(size)
        o = 0
        while o < size:
            off = min(o, size - 16)
            chunk = self.call(i, 5, bytes([sector >> 8, sector & 255, off >> 8, off & 255]))
            data[off:off + 16] = chunk[:16]
            o = off + 16
        return bytes(data)

    def write_sector(self, i, sector, data):
        n = len(data)
        self.call(i, 6, bytes([sector >> 8, sector & 255, 0, 0, n >> 8, n & 255]))
        for o in range(0, n, 16):
            self.call(i, 7, data[o:o + 16])
        self.call(i, 8)


def find_mice(hidmod):
    """Every Logitech HID++ device that answers, as (path, idx, pid)."""
    found = []
    seen = set()
    for c in hidmod.enumerate(LOGI_VID, 0):
        if c.get("usage_page") != 0xFF00 or c.get("usage") != 0x0002 or c["path"] in seen:
            continue
        seen.add(c["path"])
        for idx in (1, 2, 3, 4, 5, 6, 0xFF):
            m = None
            try:
                m = Mouse(hidmod, c["path"], idx, c["product_id"])
                m.call(0, 1, b"\x00\x00\xAA", timeout=0.4)    # ping
                if m.f(F_ONBOARD) or m.f(F_DPI):
                    found.append((c["path"], idx, c["product_id"]))
            except Exception:
                pass
            finally:
                if m:
                    m.close()
    return found


def crc_order(sec):
    """Which byte order the stored CRC uses, or None if it doesn't match."""
    c = crc16(sec[:-2])
    if sec[-2:] == struct.pack(">H", c):
        return ">"
    if sec[-2:] == struct.pack("<H", c):
        return "<"
    return None


BTN_OFF = 32          # 16 button bindings, 4 bytes each
ALT_OFF = 96          # 16 G-shift bindings (kept as they are)
MARK = b"RGBS"        # tag in the unused tail of macro sectors we wrote

SPECIALS = {0x01: "Scroll left", 0x02: "Scroll right", 0x03: "DPI up", 0x04: "DPI down",
            0x05: "DPI cycle", 0x06: "Default DPI", 0x07: "DPI shift (sniper)",
            0x08: "Next profile", 0x09: "Previous profile", 0x0A: "Cycle profiles",
            0x0B: "G-Shift", 0x0C: "Battery indicator"}
MOUSE_NAMES = {1: "Left click", 2: "Right click", 3: "Middle click", 4: "Back", 5: "Forward"}
PHYSICAL = {("mouse", 1): "Left button", ("mouse", 2): "Right button", ("mouse", 3): "Wheel click",
            ("mouse", 4): "Side button (back)", ("mouse", 5): "Side button (forward)",
            ("special", 0x05): "DPI button", ("special", 0x03): "DPI up button",
            ("special", 0x04): "DPI down button", ("special", 0x07): "Sniper button",
            ("special", 0x01): "Wheel tilt left", ("special", 0x02): "Wheel tilt right"}


def parse_profile(sec):
    # Onboard profile layout, formats 1-5: report rate (ms), default DPI index,
    # DPI-shift index, 5 DPI speeds (u16 little-endian, 0 = unused).
    dpis = [struct.unpack("<H", sec[3 + k * 2:5 + k * 2])[0] for k in range(5)]
    return {
        "rateHz": 1000 // sec[0] if 0 < sec[0] <= 8 else None,
        "defaultIndex": sec[1],
        "shiftIndex": sec[2],
        "dpis": [v for v in dpis if 0 < v < 0xFFFF],
    }


def decode_binding(b):
    """4 profile bytes -> a plain dict the UI understands."""
    raw = bytes(b).hex()
    t = b[0]
    if t == 0x80:
        if b[1] == 0x01:
            mask = (b[2] << 8) | b[3]
            if mask and mask & (mask - 1) == 0:
                return {"kind": "mouse", "btn": mask.bit_length(), "raw": raw}
        elif b[1] == 0x02:
            return {"kind": "key", "mod": b[2], "key": b[3], "raw": raw}
        elif b[1] == 0x03:
            return {"kind": "media", "usage": (b[2] << 8) | b[3], "raw": raw}
        elif b[1] == 0x00:
            return {"kind": "disabled", "raw": raw}
    elif t == 0x90:
        return {"kind": "special", "code": b[1], "raw": raw}
    elif t == 0x00:
        return {"kind": "macro", "page": b[1], "offset": b[3], "raw": raw}
    elif t == 0xFF:
        return {"kind": "disabled", "raw": raw}
    return {"kind": "raw", "raw": raw}


def encode_binding(spec, old):
    k = spec.get("kind")
    if k == "mouse":
        mask = 1 << (int(spec["btn"]) - 1)
        return bytes([0x80, 0x01, mask >> 8, mask & 255])
    if k == "key":
        return bytes([0x80, 0x02, int(spec.get("mod", 0)) & 255, int(spec.get("key", 0)) & 255])
    if k == "media":
        u = int(spec["usage"])
        return bytes([0x80, 0x03, (u >> 8) & 255, u & 255])
    if k == "special":
        return bytes([0x90, int(spec["code"]) & 255, 0x00, 0x00])
    if k == "disabled":
        return b"\xff\xff\xff\xff"
    if k == "raw":
        return bytes.fromhex(spec.get("raw") or old.hex())
    raise HidppError(f"unknown button action {k}")


# ---- macros (macro format 1: list of commands, ends with 0xFF) ------------
# 0x00 no-op (1) | 0x01-0x1F control (1) | 0x20-0x3F (2) | 0x40 delay ms BE (3)
# 0x41/0x42 mouse button down/up BE mask (3) | 0x43/0x44 key down/up mod,key (3)
# 0x45/0x46 consumer down/up (3) | 0x60-0x7F (5) | 0xFF end
def _cmd_len(op):
    if op < 0x20:
        return 1
    if op < 0x40:
        return 2
    if op < 0x60:
        return 3
    if op < 0x80:
        return 5
    return None


def decode_macro(sec, offset):
    """Macro bytes at `offset` -> UI steps, or None if it uses things we don't
    model (jumps, repeats, analog moves) - then it is left untouched."""
    steps = []
    o = offset
    while o < len(sec):
        op = sec[o]
        if op == 0xFF:
            return steps
        n = _cmd_len(op)
        if n is None or o + n > len(sec):
            return None
        a = sec[o + 1:o + n]
        if op == 0x00:
            pass
        elif op == 0x40:
            steps.append({"t": "delay", "ms": (a[0] << 8) | a[1]})
        elif op in (0x43, 0x44):
            down = op == 0x43
            mods = [0xE0 + k for k in range(8) if a[0] & (1 << k)]
            codes = (mods + ([a[1]] if a[1] else [])) if down else (([a[1]] if a[1] else []) + mods[::-1])
            steps += [{"t": "key", "code": c, "down": down} for c in codes]
        elif op in (0x41, 0x42):
            mask = (a[0] << 8) | a[1]
            if not mask or mask & (mask - 1):
                return None
            steps.append({"t": "mouse", "btn": mask.bit_length(), "down": op == 0x41})
        else:
            return None
        o += n
    return None


def encode_macro(steps):
    out = bytearray()
    for st in steps:
        t = st.get("t")
        if t == "delay":
            ms = max(1, min(65535, int(st.get("ms", 0))))
            out += bytes([0x40, ms >> 8, ms & 255])
        elif t == "key":
            c = int(st["code"]) & 255
            op = 0x43 if st.get("down") else 0x44
            if 0xE0 <= c <= 0xE7:
                out += bytes([op, 1 << (c - 0xE0), 0])     # modifiers go in the modifier byte
            else:
                out += bytes([op, 0, c])
        elif t == "mouse":
            mask = 1 << (int(st["btn"]) - 1)
            out += bytes([0x41 if st.get("down") else 0x42, mask >> 8, mask & 255])
        else:
            raise HidppError(f"unknown macro step {t}")
    out.append(0xFF)
    return bytes(out)


def _try_read(m, i, sector, size):
    try:
        return m.read_sector(i, sector, size)
    except HidppError:
        return None


def _blank(sec):
    return sec is None or sec == b"\xff" * len(sec) or sec == b"\x00" * len(sec)


def _ours(sec, size):
    return sec is not None and sec[size - 6:size - 2] == MARK and crc_order(sec)


def directory(m, i, desc):
    """User profile sectors listed in the profile directory (sector 0)."""
    d = _try_read(m, i, 0x0000, desc["size"])
    out = []
    if d and crc_order(d):
        for k in range(desc["profiles"]):
            addr = (d[4 * k] << 8) | d[4 * k + 1]
            if addr == 0xFFFF:
                break
            out.append(addr)
    return out or [desc["current"]]


def rom_labels(m, i, desc):
    """Name each physical button by what it does in the factory profile."""
    sec = _try_read(m, i, 0x0101, desc["size"])
    n = min(desc["buttons"], 16)
    labels, present, factory = [], [], []
    for k in range(n):
        label, here, b = f"Button {k + 1}", True, None
        if sec:
            raw = sec[BTN_OFF + 4 * k:BTN_OFF + 4 * k + 4]
            b = decode_binding(raw)
            key = (b["kind"], b.get("btn", b.get("code")))
            label = PHYSICAL.get(key, label)
            here = raw != b"\xff\xff\xff\xff"
            if b["kind"] in ("macro", "raw"):
                b = None
        labels.append(label)
        present.append(here)
        factory.append(b)
    return labels, present, factory


def describe(m):
    out = {"id": f"{m.pid:04x}-{m.idx}", "name": m.name(), "dpi": m.dpi_range(), "rates": m.rates()}
    try:
        i, desc = m.onboard()
        out["onboard"] = desc
        if desc["memory"] != 1 or desc["format"] > 5:
            out["why"] = f"unsupported profile format {desc['format']}"
        elif desc["current"] >= 0x0100:
            out["why"] = "the mouse is still on its factory (read-only) profile"
        else:
            sec = m.read_sector(i, desc["current"], desc["size"])
            order = crc_order(sec)
            if not order:
                out["why"] = "the profile's checksum didn't match (unknown layout) - not touching it"
            else:
                out["profile"] = parse_profile(sec)
                out["writable"] = True
                labels, present, factory = rom_labels(m, i, desc)
                buttons, cache = [], {}
                for k in range(len(labels)):
                    b = decode_binding(sec[BTN_OFF + 4 * k:BTN_OFF + 4 * k + 4])
                    if b["kind"] == "macro":
                        pg = b["page"]
                        if pg not in cache:
                            cache[pg] = _try_read(m, i, pg, desc["size"])
                        st = decode_macro(cache[pg], b["offset"]) if cache[pg] else None
                        if st is None:
                            b = {"kind": "raw", "raw": b["raw"], "note": "macro made by another app"}
                        else:
                            b["steps"] = st
                    b["label"] = labels[k]
                    b["present"] = present[k] or b["kind"] not in ("disabled",)
                    b["factory"] = factory[k]
                    buttons.append(b)
                out["buttons"] = buttons
                out["canMacro"] = desc["macro"] == 1
                out["macroRoom"] = desc["size"] - 7      # minus END, marker and CRC
            flog(f"read {out['name']} sector {desc['current']:#06x} fmt {desc['format']} "
                 f"crc {order} desc {desc}: {sec[:16].hex()} btns {sec[BTN_OFF:BTN_OFF + 4 * min(desc['buttons'], 16)].hex()}")
    except HidppError as e:
        out["why"] = str(e)
    return out


def _backup(backup_dir, pid, sector, data):
    os.makedirs(backup_dir, exist_ok=True)
    bk = os.path.join(backup_dir, f"logi-{pid:04x}-sector{sector}.bin")
    if data is not None and not os.path.exists(bk):    # keep the very first original
        with open(bk, "wb") as f:
            f.write(data)
    return bk


def _write_verified(m, i, sector, data):
    m.write_sector(i, sector, data)
    if m.read_sector(i, sector, len(data)) != data:
        flog(f"readback mismatch after writing sector {sector:#06x}")
        raise HidppError("the mouse didn't store it correctly - use Restore")


def _place_macros(m, i, desc, old, cur, buttons, backup_dir, order):
    """Write every macro the buttons need; returns {index: (page, offset)}."""
    size = desc["size"]
    room = size - 6                       # bytes before the marker + CRC
    want = {}                             # encoded bytes -> [button indexes]
    for k, spec in enumerate(buttons):
        if spec and spec.get("kind") == "macro":
            code = encode_macro(spec.get("steps") or [])
            if len(code) > room:
                raise HidppError(f"the macro on button {k + 1} is too long ({len(code)} of {room} bytes)")
            want.setdefault(code, []).append(k)
    if not want:
        return {}
    if desc["macro"] != 1:
        raise HidppError("this mouse can't store macros")
    # Sectors we must not touch: other profiles' macros and G-shift macros.
    profiles = set(directory(m, i, desc)) | {cur}
    reserved = set(profiles) | {0}
    for p in profiles:
        sec = old if p == cur else _try_read(m, i, p, size)
        if not sec or not crc_order(sec):
            continue
        for off in ([ALT_OFF] if p == cur else [BTN_OFF, ALT_OFF]):
            for k in range(16):
                b = sec[off + 4 * k:off + 4 * k + 4]
                if b[0] == 0x00:
                    reserved.add(b[1])
    # Macros on this profile's main buttons we can't read stay where they are.
    mine = []
    for k in range(min(desc["buttons"], 16)):
        b = old[BTN_OFF + 4 * k:BTN_OFF + 4 * k + 4]
        if b[0] == 0x00:
            keep = buttons[k] and buttons[k].get("kind") == "raw"
            (reserved.add if keep else mine.append)(b[1])
    # Pack the macros into as few sectors as possible.
    chunks, cur_chunk = [], bytearray()
    where = {}
    for code, idxs in want.items():
        if len(cur_chunk) + len(code) > room:
            chunks.append(cur_chunk)
            cur_chunk = bytearray()
        for k in idxs:
            where[k] = (len(chunks), len(cur_chunk))
        cur_chunk += code
    chunks.append(cur_chunk)
    # Pick sectors: this profile's old macro sectors / ours first, then blank ones from the top.
    last = max(desc["sectors"], 2) - 1
    picks = []
    for s in mine + list(range(last, max(profiles) , -1)):
        if len(picks) == len(chunks):
            break
        if s in reserved or s in picks or not (0 < s <= last) or s in profiles:
            continue
        sec = _try_read(m, i, s, size)
        if s in mine or _ours(sec, size) or _blank(sec):
            picks.append(s)
            if sec and crc_order(sec) and not _ours(sec, size):   # someone else's data
                _backup(backup_dir, m.pid, s, sec)
    if len(picks) < len(chunks):
        raise HidppError("no free space in the mouse's memory for macros")
    for n, chunk in enumerate(chunks):
        data = bytearray(b"\xff" * size)
        data[:len(chunk)] = chunk
        data[size - 6:size - 2] = MARK
        data[-2:] = struct.pack(order + "H", crc16(bytes(data[:-2])))
        data = bytes(data)
        if _try_read(m, i, picks[n], size) != data:
            _write_verified(m, i, picks[n], data)
        flog(f"macro sector {picks[n]:#06x}: {chunk.hex()}")
    return {k: (picks[c], off) for k, (c, off) in where.items()}


def save(m, req, backup_dir):
    i, desc = m.onboard()
    if desc["memory"] != 1 or desc["format"] > 5 or desc["current"] >= 0x0100:
        raise HidppError("this mouse's profile can't be edited safely")
    size, sector = desc["size"], desc["current"]
    old = m.read_sector(i, sector, size)
    order = crc_order(old)
    if not order:
        raise HidppError("profile checksum mismatch - not touching it")
    dpis = [int(v) for v in (req.get("dpis") or [])][:5]
    if not dpis:
        raise HidppError("need at least one DPI speed")
    rng = m.dpi_range() or {"min": 100, "max": 25600, "step": 50}
    for v in dpis:
        if not (rng["min"] <= v <= rng["max"]):
            raise HidppError(f"{v} DPI is outside {rng['min']}-{rng['max']}")
    bk = _backup(backup_dir, m.pid, sector, old)
    new = bytearray(old)
    rate_hz = req.get("rateHz")
    if rate_hz:
        new[0] = max(1, min(8, round(1000 / int(rate_hz))))
    new[1] = max(0, min(len(dpis) - 1, int(req.get("defaultIndex", 0))))
    if req.get("shiftIndex") is not None:
        new[2] = int(req["shiftIndex"])
    if new[2] >= len(dpis):                            # DPI-shift speed must exist
        new[2] = new[1]
    for k in range(5):
        struct.pack_into("<H", new, 3 + k * 2, dpis[k] if k < len(dpis) else 0)
    buttons = req.get("buttons")
    if buttons is not None:
        n = min(desc["buttons"], 16)
        buttons = (list(buttons) + [None] * n)[:n]
        if not any(b and b.get("kind") == "mouse" and int(b.get("btn", 0)) == 1 for b in buttons):
            raise HidppError("keep Left click on at least one button")
        spots = _place_macros(m, i, desc, old, sector, buttons, backup_dir, order)
        for k, spec in enumerate(buttons):
            o = BTN_OFF + 4 * k
            if not spec:
                continue
            if spec.get("kind") == "macro":
                pg, off = spots[k]
                new[o:o + 4] = bytes([0x00, pg, 0x00, off])
            else:
                new[o:o + 4] = encode_binding(spec, bytes(old[o:o + 4]))
    new[-2:] = struct.pack(order + "H", crc16(bytes(new[:-2])))
    if bytes(new) == old:
        return {"changed": False}
    _write_verified(m, i, sector, bytes(new))
    try:
        m.call(i, 3, bytes([sector >> 8, sector & 255]))  # reload the profile
    except HidppError:
        pass
    flog(f"saved sector {sector:#06x}: dpis {dpis} default {new[1]} shift {new[2]} rate {rate_hz} "
         f"btns {bytes(new[BTN_OFF:BTN_OFF + 4 * min(desc['buttons'], 16)]).hex()}")
    return {"changed": True, "backup": bk}


def restore(m, backup_dir):
    i, desc = m.onboard()
    sector = desc["current"]
    bk = os.path.join(backup_dir, f"logi-{m.pid:04x}-sector{sector}.bin")
    if not os.path.exists(bk):
        raise HidppError("no backup for this mouse yet (nothing was changed)")
    data = open(bk, "rb").read()
    if len(data) != desc["size"] or not crc_order(data):
        raise HidppError("the backup file is damaged")
    _write_verified(m, i, sector, data)                # profile first: drops our macro links
    try:
        m.call(i, 3, bytes([sector >> 8, sector & 255]))
    except HidppError:
        pass
    # Put back macro sectors that held something before (blank ones can stay).
    prefix = f"logi-{m.pid:04x}-sector"
    for f in os.listdir(backup_dir):
        if not f.startswith(prefix) or not f.endswith(".bin"):
            continue
        try:
            s = int(f[len(prefix):-4])
        except ValueError:
            continue
        if s == sector:
            continue
        d = open(os.path.join(backup_dir, f), "rb").read()
        if len(d) == desc["size"] and crc_order(d):
            try:
                _write_verified(m, i, s, d)
            except HidppError as e:
                flog(f"restore sector {s}: {e}")
    return {"restored": True}


def serve():
    hidmod = _load_hid()
    print(json.dumps({"ok": True, "event": "ready"}), flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = {}
        try:
            req = json.loads(line)
            cmd = req.get("cmd")
            if cmd == "quit":
                break
            if cmd == "scan":
                mice = []
                for path, idx, pid in find_mice(hidmod):
                    m = Mouse(hidmod, path, idx, pid)
                    try:
                        mice.append(describe(m))
                    finally:
                        m.close()
                print(json.dumps({"ok": True, "mice": mice}), flush=True)
                continue
            if cmd in ("save", "restore"):
                target = None
                for path, idx, pid in find_mice(hidmod):
                    if f"{pid:04x}-{idx}" == req.get("id"):
                        target = (path, idx, pid)
                if not target:
                    raise HidppError("mouse not found - move it to wake it up")
                m = Mouse(hidmod, *target)
                try:
                    if cmd == "save":
                        res = save(m, req, req["backupDir"])
                    else:
                        res = restore(m, req["backupDir"])
                    res["mouse"] = describe(m)
                finally:
                    m.close()
                print(json.dumps({"ok": True, **res}), flush=True)
                continue
            print(json.dumps({"ok": False, "error": f"unknown cmd {cmd}"}), flush=True)
        except Exception as e:
            flog(f"{req.get('cmd')}: {e}")
            print(json.dumps({"ok": False, "error": str(e)}), flush=True)
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        sys.exit(serve())
    print(__doc__)
