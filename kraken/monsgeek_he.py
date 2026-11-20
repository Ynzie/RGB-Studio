#!/usr/bin/env python3
"""
monsgeek_he.py - RGB Studio driver for the MonsGeek M1 V5 HE (RongYuan RY5088).

STOCK FIRMWARE, NO FLASHING. Uses the keyboard's own "Screen Sync" LED mode
(mode 21), which is designed for a host to stream a live RGB color
(SET_SCREEN_COLOR 0x0E, ~50 Hz capable, RAM only - no flash wear).

Protocol reverse-engineered by echtzeit-solutions/monsgeek-akko-linux (GPL-3.0),
docs/PROTOCOL.md: 64-byte feature reports, report ID 0, vendor interface 2
(usage page 0xFFFF, usage 0x02).

REQUIREMENTS:  py -m pip install hidapi
Close the MonsGeek driver / app.monsgeek.com while RGB Studio controls it.

USAGE
  py monsgeek_he.py probe            # find the keyboard
  py monsgeek_he.py color RRGGBB     # screen-sync mode + one color
  py monsgeek_he.py restore RRGGBB   # back to the keyboard's own static mode
  py monsgeek_he.py serve            # JSON-lines loop used by RGB Studio
"""

import sys
import json
import time
import os

# Small rolling log next to this file so problems inside RGB Studio are visible.
_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monsgeek.log")


def mlog(msg):
    try:
        if os.path.exists(_LOG) and os.path.getsize(_LOG) > 512 * 1024:
            os.replace(_LOG, _LOG + ".old")   # keep it small
        with open(_LOG, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


try:
    import hid
except Exception as e:  # pragma: no cover
    mlog("IMPORT FAILED: hidapi missing for %s (%s)" % (sys.executable, e))
    print(json.dumps({"ok": False, "event": "error",
                      "error": "Python package 'hidapi' missing. Run: py -m pip install hidapi (%s)" % e}),
          flush=True)
    sys.exit(2)

VID = 0x3151
PIDS = {0x5030: "M1 V5 HE (wired)"}   # wired only: 2.4G dongle uses another format

SET_LEDPARAM = 0x07       # checksum "Bit8"
SET_SCREEN_COLOR = 0x0E   # checksum "Bit7"
MODE_CONSTANT = 1
MODE_SCREEN_SYNC = 21
DAZZLE_OFF = 8            # single color (not rainbow cycle)
BRIGHTNESS_MAX = 4


def _checksum_bit7(buf):
    buf[7] = (255 - (sum(buf[0:7]) & 0xFF)) & 0xFF


def _checksum_bit8(buf):
    buf[8] = (255 - (sum(buf[0:8]) & 0xFF)) & 0xFF


class MonsGeekHE:
    def __init__(self):
        self.dev = None
        self.name = None

    def open(self):
        cands = []
        for d in hid.enumerate(VID, 0):
            if d.get("product_id") not in PIDS:
                continue
            # vendor config interface: usage page 0xFFFF / usage 0x02 (interface 2)
            if d.get("usage_page") == 0xFFFF or d.get("interface_number") == 2:
                cands.append(d)
        if not cands:
            raise RuntimeError("MonsGeek M1 V5 HE not found (plug it in with the cable, wired mode).")
        cands.sort(key=lambda d: (d.get("usage_page") != 0xFFFF, d.get("usage") != 0x02))
        info = cands[0]
        dev = hid.device()
        dev.open_path(info["path"])
        self.dev = dev
        self.name = PIDS.get(info.get("product_id"), "MonsGeek HE")
        return self

    def close(self):
        if self.dev is not None:
            try:
                self.dev.close()
            except Exception:
                pass
            self.dev = None

    def _send(self, buf64):
        # Windows hidapi: first byte = report ID (0), then the 64-byte report.
        n = self.dev.send_feature_report(bytes([0]) + bytes(buf64))
        if n is None or n < 0:
            raise RuntimeError("feature report write failed (keyboard unplugged or busy?)")

    def set_led(self, mode, r=0, g=0, b=0, brightness=BRIGHTNESS_MAX, speed=2):
        buf = bytearray(64)
        buf[0] = SET_LEDPARAM
        buf[1] = mode
        buf[2] = 4 - max(0, min(4, speed))        # speed is inverted on the wire
        buf[3] = max(0, min(BRIGHTNESS_MAX, brightness))
        buf[4] = DAZZLE_OFF
        buf[5], buf[6], buf[7] = r, g, b
        _checksum_bit8(buf)
        self._send(buf)

    def enter_sync(self):
        self.set_led(MODE_SCREEN_SYNC, 255, 255, 255)
        time.sleep(0.05)

    def stream_color(self, r, g, b):
        buf = bytearray(64)
        buf[0] = SET_SCREEN_COLOR
        buf[1], buf[2], buf[3] = r, g, b
        _checksum_bit7(buf)
        self._send(buf)


GET_LEDPARAM = 0x87


# ---------------------------------------------------------------------------
# Keyboard settings for RGB Studio's Peripherals page: actuation / release
# points, Rapid Trigger, dead zones, polling rate, key remaps and macros.
# All stored on the keyboard (active profile), so they work with nothing running.
#
# Wire format from echtzeit-solutions/monsgeek-akko-linux (GPL-3.0) - the
# firmware-validated driver for this board (docs/PROTOCOL.md, key_action.rs,
# command.rs). The firmware does NO bounds checking on these commands, so every
# index is range-checked here before it goes out (profile 0-3, key 0-125,
# macro slot 0-15, chunk page 0-9).
# ---------------------------------------------------------------------------
GET_REPORT, SET_REPORT = 0x83, 0x03
GET_PROFILE = 0x84
GET_KEYMATRIX, SET_KEYMATRIX = 0x8A, 0x0A
SET_MACRO = 0x0B
GET_USB_VERSION = 0x8F
GET_MULTI_MAG, SET_MULTI_MAG = 0xE5, 0x65
GET_FEATURES = 0xE6
POSITIONS = 126                     # column-major matrix, 6 rows per column
MAG_U16 = (("press", 0x00), ("lift", 0x01), ("rtPress", 0x02), ("rtLift", 0x03),
           ("bdz", 0x06), ("tdz", 0xFB))
KEY_MODE, RT_FLAG = 0x07, 0x80
POLL_HZ = [8000, 4000, 2000, 1000, 500, 250, 125]
MACRO_SLOTS = list(range(15, -1, -1))   # we use slots from the top; MonsGeek's app fills from 0
SETTLE = 0.25                        # after a flash commit, before reading back

# M1 V5 layout, matrix position -> key (monsgeek-akko-linux protocol.rs, verified
# against the factory keymatrix). "?" = no key at that position.
KEY_NAMES = [
    "Esc", "`", "Tab", "Caps", "LShf", "LCtl",   "F1", "1", "Q", "A", "IntlBs", "Win",
    "F2", "2", "W", "S", "Z", "LAlt",            "F3", "3", "E", "D", "X", "?",
    "F4", "4", "R", "F", "C", "?",               "F5", "5", "T", "G", "V", "?",
    "F6", "6", "Y", "H", "B", "Spc",             "F7", "7", "U", "J", "N", "?",
    "F8", "8", "I", "K", "M", "?",               "F9", "9", "O", "L", ",", "RAlt",
    "F10", "0", "P", ";", ".", "Fn",             "F11", "-", "[", "'", "/", "RCtl",
    "F12", "=", "]", "IntlRo", "RShf", "Left",   "Del", "Bksp", "\\", "Ent", "Up", "Down",
    "?", "Home", "PgUp", "PgDn", "End", "Right",
]


def _cmd(cmd, params=(), payload=b""):
    """64-byte command: [cmd, p1..p6, checksum, payload...]."""
    buf = bytearray(64)
    buf[0] = cmd
    for i, v in enumerate(list(params)[:6]):
        buf[1 + i] = v & 0xFF
    _checksum_bit7(buf)
    buf[8:8 + len(payload)] = bytes(payload)[:56]
    return buf


def _u16s(raw, n=POSITIONS):
    return [raw[2 * i] | (raw[2 * i + 1] << 8) for i in range(n)]


def _hexb(b):
    return bytes(b).hex()


def decode_cfg(c, mine):
    """4-byte keymatrix entry -> the binding format the UI shares with the mouse."""
    t, b1, b2, b3 = c
    if t == 0:
        us = [u for u in (b1, b2, b3) if u >= 4]
        if not us:
            return {"kind": "disabled"}
        mods = [u for u in us if 0xE0 <= u <= 0xE7]
        keys = [u for u in us if not 0xE0 <= u <= 0xE7]
        if len(keys) <= 1:
            mod = 0
            for u in mods:
                mod |= 1 << (u - 0xE0)
            return {"kind": "key", "mod": mod, "key": keys[0] if keys else 0}
    elif t == 1 and b2 in (1, 2):
        return {"kind": "mouse", "btn": b2}
    elif t == 3:
        return {"kind": "media", "usage": b2 | (b3 << 8)}
    elif t == 9:
        steps = mine.get(str(b2))
        if steps is not None:
            return {"kind": "macro", "steps": steps, "slot": b2}
        return {"kind": "raw", "raw": _hexb(c), "note": "macro made in the MonsGeek app"}
    elif t == 10 and b1 == 1:
        return {"kind": "raw", "raw": _hexb(c), "note": "Fn key", "fn": True}
    return {"kind": "raw", "raw": _hexb(c), "note": "setting made in the MonsGeek app"}


def encode_cfg(spec, slot=None):
    k = spec.get("kind")
    if k == "key":
        us = [0xE0 + b for b in range(8) if int(spec.get("mod", 0)) & (1 << b)]
        if spec.get("key"):
            us.append(int(spec["key"]) & 0xFF)
        if not us:
            return [0, 0, 0, 0]
        if len(us) > 3:
            raise RuntimeError("a key can press at most 3 keys at once (Ctrl/Shift/Alt/Win count)")
        return [0, 0, us[0], 0] if len(us) == 1 else [0] + (us + [0, 0])[:3]
    if k == "mouse":
        b = int(spec.get("btn", 1))
        if b not in (1, 2):
            raise RuntimeError("the keyboard can only do left or right click")
        return [1, 0, b, 0]
    if k == "media":
        u = int(spec["usage"])
        return [3, 0, u & 0xFF, (u >> 8) & 0xFF]
    if k == "disabled":
        return [0, 0, 0, 0]
    if k == "macro":
        return [9, 0, int(slot), 0]
    if k == "raw":
        return list(bytes.fromhex(spec["raw"]))
    raise RuntimeError("the keyboard can't do that action (%s)" % k)


def encode_kb_macro(steps):
    """Keyboard macro: [repeat u16 LE] then per event [key, down<<7 | delay] or
    [key, down<<7, delay u16 LE] for 0 / 128+ ms, ending with [0, 0]."""
    evs = []
    for st in steps or []:
        t = st.get("t")
        if t == "delay":
            if evs:
                evs[-1][2] += int(st.get("ms", 0))
        elif t == "key":
            evs.append([int(st["code"]) & 0xFF, bool(st.get("down")), 0])
        elif t == "mouse":
            raise RuntimeError("mouse clicks inside a macro only work on the mouse")
    out = bytearray([1, 0])                       # play once
    for code, down, d in evs:
        d = max(0, min(65535, d))
        if down and d == 0:
            d = 5                                 # give the press a moment
        flag = 0x80 if down else 0
        if 1 <= d <= 127:
            out += bytes([code, flag | d])
        else:
            out += bytes([code, flag, d & 0xFF, d >> 8])
    out += b"\x00\x00"
    if len(out) > 256:
        raise RuntimeError("macro too long for the keyboard (%d of 256 bytes)" % len(out))
    while len(out) < 56:
        out.append(0)
    return bytes(out)


class KbSettings:
    """Reads and writes the keyboard's own settings through an open MonsGeekHE."""

    def __init__(self, kb):
        self.kb = kb

    # -- raw I/O ------------------------------------------------------------
    def _read(self):
        r = bytes(self.kb.dev.get_feature_report(0, 65))
        return r[1:] if len(r) == 65 else r

    def query(self, buf, echo=None, tries=5):
        for _ in range(tries):
            self.kb._send(buf)
            time.sleep(0.012)
            r = self._read()
            if len(r) >= 64 and (echo is None or r[0] == echo):
                return r[:64]
        raise RuntimeError("keyboard didn't answer (0x%02x)" % buf[0])

    def page(self, buf):
        """Paged answers carry no echo: take one only when two reads agree."""
        last = None
        for _ in range(6):
            r = self.query(buf)
            if r == last:
                return r
            last = r
        raise RuntimeError("keyboard gave unstable answers (0x%02x)" % buf[0])

    def write(self, buf, wait=0.03):
        self.kb._send(buf)
        time.sleep(wait)

    # -- reads --------------------------------------------------------------
    def info(self):
        v = self.query(_cmd(GET_USB_VERSION), GET_USB_VERSION)
        dev_id = v[1] | (v[2] << 8) | (v[3] << 16) | (v[4] << 24)
        version = v[8] | (v[9] << 8)
        factor = None
        try:
            f = self.query(_cmd(GET_FEATURES), GET_FEATURES)
            if f[1] == 0xAA:
                factor = {0: 10, 1: 100, 2: 200}.get(f[2])
        except RuntimeError:
            pass
        if not factor:
            factor = 200 if version >= 1280 else 100 if version >= 768 else 10
        profile = self.query(_cmd(GET_PROFILE), GET_PROFILE)[1]
        if profile > 3:
            raise RuntimeError("keyboard reported profile %d" % profile)
        code = self.query(_cmd(GET_REPORT), GET_REPORT)[2]
        return {"devId": dev_id, "version": version, "factor": factor, "profile": profile,
                "polling": POLL_HZ[code] if code < len(POLL_HZ) else None}

    def table16(self, sub):
        raw = b"".join(self.page(_cmd(GET_MULTI_MAG, (sub, 1, p))) for p in range(4))
        return _u16s(raw)

    def modes(self):
        return list(b"".join(self.page(_cmd(GET_MULTI_MAG, (KEY_MODE, 1, p))) for p in range(2))[:POSITIONS])

    def keymatrix(self, profile):
        raw = b"".join(self.page(_cmd(GET_KEYMATRIX, (profile, 0xFF, p, 0))) for p in range(8))
        return [list(raw[4 * i:4 * i + 4]) for i in range(POSITIONS)]

    def snapshot(self):
        st = self.info()
        for name, sub in MAG_U16:
            try:
                st[name] = self.table16(sub)
            except RuntimeError:
                if name in ("bdz", "tdz"):
                    st[name] = None             # older firmware: no dead zones
                    continue
                raise
        st["mode"] = self.modes()
        st["keymatrix"] = self.keymatrix(st["profile"])
        return st

    # -- writes -------------------------------------------------------------
    def write_table16(self, sub, values):
        data = b"".join(bytes([v & 0xFF, (v >> 8) & 0xFF]) for v in values[:POSITIONS])
        pages = [data[i:i + 56] for i in range(0, len(data), 56)]
        for p, chunk in enumerate(pages):
            last = p == len(pages) - 1
            self.write(_cmd(SET_MULTI_MAG, (sub, 1, p, 1 if last else 0, 0, 0), chunk),
                       SETTLE if last else 0.03)

    def write_modes(self, changes):
        items = sorted(changes.items())
        for n, (pos, mode) in enumerate(items):
            last = n == len(items) - 1
            self.write(_cmd(SET_MULTI_MAG, (KEY_MODE, 0, pos, 1 if last else 0, 0, 0), [mode]),
                       SETTLE if last else 0.03)

    def write_key(self, profile, pos, cfg):
        if not (0 <= profile <= 3 and 0 <= pos <= 125):
            raise RuntimeError("key position out of range")
        self.write(_cmd(SET_KEYMATRIX, (profile, pos, 0, 0, 1, 0), cfg), SETTLE)

    def write_macro(self, slot, data):
        if slot not in MACRO_SLOTS:
            raise RuntimeError("macro slot out of range")
        pages = [data[i:i + 56] for i in range(0, len(data), 56)]
        if len(pages) > 9:
            raise RuntimeError("macro too long")
        for p, chunk in enumerate(pages):
            last = p == len(pages) - 1
            self.write(_cmd(SET_MACRO, (slot, p, len(chunk), 1 if last else 0, 0, 0), chunk),
                       SETTLE if last else 0.03)

    def write_polling(self, hz):
        if hz not in POLL_HZ:
            raise RuntimeError("unsupported polling rate %s" % hz)
        self.write(_cmd(SET_REPORT, (0, POLL_HZ.index(hz))), 0.1)


def _state_path(backup_dir, dev_id, what):
    os.makedirs(backup_dir, exist_ok=True)
    return os.path.join(backup_dir, "monsgeek-%08x-%s.json" % (dev_id, what))


def _load_json(p, default):
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(p, data):
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, p)


def _visible(pos, km):
    name = KEY_NAMES[pos] if pos < len(KEY_NAMES) else "?"
    if name == "?":
        return False
    if name in ("IntlBs", "IntlRo") and km[pos] == [0, 0, 0, 0]:
        return False                              # ANSI board: no key there
    return True


def kb_scan(ks, backup_dir):
    st = ks.snapshot()
    bk = _state_path(backup_dir, st["devId"], "p%d-original" % st["profile"])
    if not os.path.exists(bk):                    # first time: keep the original
        _save_json(bk, st)
    orig = _load_json(bk, st)
    mine = _load_json(_state_path(backup_dir, st["devId"], "macros"), {})
    keys = []
    for pos in range(min(POSITIONS, len(KEY_NAMES))):
        if not _visible(pos, orig.get("keymatrix") or st["keymatrix"]):
            continue
        b = decode_cfg(st["keymatrix"][pos], mine)
        ob = decode_cfg((orig.get("keymatrix") or st["keymatrix"])[pos], mine)
        k = {"pos": pos, "name": KEY_NAMES[pos], "row": pos % 6, "col": pos // 6,
             "binding": b, "orig": ob, "fn": bool(b.get("fn") or ob.get("fn")),
             "rt": bool(st["mode"][pos] & RT_FLAG), "mode": st["mode"][pos] & 0x7F}
        for name, _sub in MAG_U16:
            k[name] = st[name][pos] if st[name] else None
        keys.append(k)
    mlog("kb scan: dev %08x fw %04x factor %d profile %d polling %s, %d keys"
         % (st["devId"], st["version"], st["factor"], st["profile"], st["polling"], len(keys)))
    return {"keyboard": {
        "id": "mg-%08x" % st["devId"], "name": "MonsGeek M1 V5 HE", "profile": st["profile"],
        "factor": st["factor"], "firmware": "v%d%02d" % (st["version"] >> 8, st["version"] & 0xFF),
        "polling": st["polling"], "pollRates": POLL_HZ, "keys": keys,
        "deadzones": st["bdz"] is not None, "macroRoom": 256 - 4}}


def _apply(ks, st, want, backup_dir):
    """Write only what differs from the keyboard's current state `st`."""
    prof = st["profile"]
    done = []
    # 1) magnetism tables
    for name, sub in MAG_U16:
        ch = (want.get("mag") or {}).get(name) or {}
        if not ch or st[name] is None:
            continue
        new = list(st[name])
        for pos, v in ch.items():
            pos = int(pos)
            if 0 <= pos < POSITIONS:
                new[pos] = max(0, min(0xFFFF, int(v)))
        if new != st[name]:
            ks.write_table16(sub, new)
            done.append(name)
    # 2) rapid trigger flags (one key at a time, as the vendor app does)
    modes = {}
    for pos, on in (want.get("rt") or {}).items():
        pos = int(pos)
        cur = st["mode"][pos]
        new = (cur | RT_FLAG) if on else (cur & ~RT_FLAG & 0xFF)
        if new != cur:
            modes[pos] = new
    if modes:
        ks.write_modes(modes)
        done.append("rt")
    # 3) key remaps (macros first, so a key never points at an empty slot)
    keys = {int(p): s for p, s in (want.get("keys") or {}).items()}
    if keys:
        mine_p = _state_path(backup_dir, st["devId"], "macros")
        mine = _load_json(mine_p, {})
        final = {}
        for pos in range(POSITIONS):
            final[pos] = keys.get(pos)
        reserved = set()
        for pos, cfg in enumerate(st["keymatrix"]):
            if cfg[0] == 9 and (final[pos] is None or final[pos].get("kind") == "raw"):
                reserved.add(cfg[2])              # a macro staying on a key we don't touch
        slot_of = {}
        for pos, spec in keys.items():
            if spec and spec.get("kind") == "macro":
                data = encode_kb_macro(spec.get("steps"))
                if data in slot_of:
                    continue
                key = json.dumps(spec.get("steps"))
                have = [int(s) for s, v in mine.items() if json.dumps(v) == key and int(s) not in slot_of.values()]
                if have:
                    slot_of[data] = have[0]
                    continue
                # our own slots no key still uses first, then ones we've never written
                free = [s for s in MACRO_SLOTS if s not in reserved and s not in slot_of.values()
                        and str(s) in mine]
                free += [s for s in MACRO_SLOTS if s not in reserved and s not in slot_of.values()
                         and str(s) not in mine]
                if not free:
                    raise RuntimeError("no free macro slots on the keyboard")
                slot_of[data] = free[0]
                ks.write_macro(free[0], data)
                mine[str(free[0])] = spec.get("steps")
                _save_json(mine_p, mine)
        for pos, spec in sorted(keys.items()):
            if not spec or not (0 <= pos < POSITIONS) or not _visible(pos, st["keymatrix"]):
                continue
            cur = st["keymatrix"][pos]
            if cur[0] == 10 and cur[1] == 1:
                continue                          # never remap the Fn key
            slot = slot_of.get(encode_kb_macro(spec.get("steps"))) if spec.get("kind") == "macro" else None
            cfg = encode_cfg(spec, slot)
            if cfg != cur:
                ks.write_key(prof, pos, cfg)
                done.append("key %s" % KEY_NAMES[pos])
    # 4) polling rate
    hz = want.get("polling")
    if hz and hz != st["polling"]:
        ks.write_polling(int(hz))
        done.append("polling")
    return done


def kb_save(ks, want, backup_dir):
    st = ks.snapshot()
    bk = _state_path(backup_dir, st["devId"], "p%d-original" % st["profile"])
    if not os.path.exists(bk):
        _save_json(bk, st)
    done = _apply(ks, st, want, backup_dir)
    if not done:
        return {"changed": False}
    after = ks.snapshot()                         # read back and check it stuck
    bad = []
    for name, _sub in MAG_U16:
        for pos, v in ((want.get("mag") or {}).get(name) or {}).items():
            if after[name] is not None and after[name][int(pos)] != int(v):
                bad.append("%s %s" % (name, KEY_NAMES[int(pos)]))
    for pos, on in (want.get("rt") or {}).items():
        if bool(after["mode"][int(pos)] & RT_FLAG) != bool(on):
            bad.append("rt %s" % KEY_NAMES[int(pos)])
    mlog("kb save: %s%s" % (", ".join(done), (" | MISMATCH " + ", ".join(bad[:8])) if bad else ""))
    if bad:
        raise RuntimeError("some settings didn't stick (%s) - try Save again" % ", ".join(bad[:4]))
    return {"changed": True}


def kb_restore(ks, backup_dir):
    st = ks.snapshot()
    orig = _load_json(_state_path(backup_dir, st["devId"], "p%d-original" % st["profile"]), None)
    if not orig:
        raise RuntimeError("no backup yet (nothing was changed)")
    want = {"mag": {}, "rt": {}, "keys": {}, "polling": orig.get("polling")}
    for name, _sub in MAG_U16:
        if orig.get(name) and st[name]:
            want["mag"][name] = {p: v for p, v in enumerate(orig[name]) if v != st[name][p]}
    for p in range(POSITIONS):
        if bool(orig["mode"][p] & RT_FLAG) != bool(st["mode"][p] & RT_FLAG):
            want["rt"][p] = bool(orig["mode"][p] & RT_FLAG)
        if orig["keymatrix"][p] != st["keymatrix"][p] and _visible(p, orig["keymatrix"]):
            want["keys"][p] = {"kind": "raw", "raw": _hexb(orig["keymatrix"][p])}
    _apply(ks, st, want, backup_dir)
    mlog("kb restore done")
    return {"restored": True}


def diag():
    """Read-mostly diagnostics: list interfaces, read LED params, try a static
    color and read back. Only writes one ordinary LED setting (mode 1)."""
    print("== HID interfaces for VID 3151 ==")
    devs = [d for d in hid.enumerate(VID, 0)]
    if not devs:
        print("  none found - keyboard not visible to hidapi")
        return 1
    for d in devs:
        print("  pid=%04x iface=%s usage_page=%04x usage=%04x path=%s" % (
            d.get("product_id", 0), d.get("interface_number"), d.get("usage_page", 0),
            d.get("usage", 0), d.get("path")))
    kb = MonsGeekHE().open()
    print("opened:", kb.name)

    def query(cmd):
        buf = bytearray(64)
        buf[0] = cmd
        _checksum_bit7(buf)
        kb._send(buf)
        for _ in range(4):
            time.sleep(0.08)
            r = kb.dev.get_feature_report(0, 65)
            data = bytes(r)
            if data and data[0] == 0:   # strip report id if hidapi returned it
                data = data[1:]
            if data and data[0] == cmd:
                return data
        return data

    r = query(GET_LEDPARAM)
    print("LED params BEFORE:", r[:10].hex(" ") if r else None,
          "(byte0 should be 87, byte1 = mode)")
    kb.set_led(MODE_CONSTANT, 255, 0, 170)
    time.sleep(0.2)
    r = query(GET_LEDPARAM)
    print("LED params AFTER static pink:", r[:10].hex(" ") if r else None,
          "(expect: 87 01 .. .. .. ff 00 aa)")
    kb.close()
    return 0


def _parse(hexs):
    h = (hexs or "").lstrip("#")
    if len(h) < 6:
        return 0, 0, 0
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def serve():
    """One JSON command per line on stdin.
      {"cmd":"color","hex":"RRGGBB"}   stream a color (no reply - high rate)
      {"cmd":"restore","hex":"RRGGBB"} leave app control: static color on-board
      {"cmd":"quit"}
    Emits {"event":"ready"} once the keyboard is open, or {"event":"error"}.
    """
    import queue
    import threading

    mlog("=== serve start (python %s)" % sys.executable)
    kb = MonsGeekHE()
    q = queue.Queue()
    state = {}  # last_open_err: dedupe repeated "open failed" log lines

    # stdin reader thread, so the main loop can keep retrying the keyboard
    # even when RGB Studio isn't sending anything (it only streams colors
    # once the keyboard reports ready - waiting on stdin alone deadlocked).
    def reader():
        for line in sys.stdin:
            q.put(line)
        q.put(None)  # stdin closed -> app gone
    threading.Thread(target=reader, daemon=True).start()

    def try_open():
        try:
            kb.open()
            kb.enter_sync()
            mlog("opened %s, screen-sync mode on" % kb.name)
            state.pop("last_open_err", None)
            print(json.dumps({"ok": True, "event": "ready", "name": kb.name}), flush=True)
            return True
        except Exception as e:
            kb.close()
            # Retried every 3 s: only log when the reason changes, so an
            # unplugged keyboard doesn't fill the log.
            if str(e) != state.get("last_open_err"):
                mlog("open failed: %s" % e)
                state["last_open_err"] = str(e)
            print(json.dumps({"ok": False, "event": "error", "error": str(e)}), flush=True)
            return False

    in_sync = try_open()
    last = (0, 0, 0)
    last_try = time.time()
    sent = 0
    while True:
        # Not connected: retry every 3 s (boot race, sleep/wake, replug).
        # (kb_* settings commands run inline here, so they never interleave
        # with the color stream on the same feature report.)
        if kb.dev is None and time.time() - last_try > 3.0:
            last_try = time.time()
            in_sync = try_open()
        try:
            line = q.get(timeout=0.5)
        except queue.Empty:
            continue
        if line is None:
            break
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            cmd = req.get("cmd")
            if cmd == "quit":
                break
            if kb.dev is None:
                continue                 # retry loop above handles reconnect
            if cmd in ("kb_scan", "kb_save", "kb_restore"):
                rid = req.get("rid")
                try:
                    if kb.dev is None:
                        raise RuntimeError("keyboard not connected (wired mode, cable plugged in)")
                    ks = KbSettings(kb)
                    bdir = req.get("backupDir") or os.path.dirname(os.path.abspath(__file__))
                    if cmd == "kb_scan":
                        res = kb_scan(ks, bdir)
                    elif cmd == "kb_save":
                        res = kb_save(ks, req.get("want") or {}, bdir)
                        res.update(kb_scan(ks, bdir))
                    else:
                        res = kb_restore(ks, bdir)
                        res.update(kb_scan(ks, bdir))
                    print(json.dumps(dict(res, rid=rid, ok=True)), flush=True)
                except Exception as e:
                    mlog("%s failed: %s" % (cmd, e))
                    print(json.dumps({"rid": rid, "ok": False, "error": str(e)}), flush=True)
                continue
            if cmd == "color":
                if not in_sync:
                    kb.enter_sync()
                    in_sync = True
                last = _parse(req.get("hex"))
                kb.stream_color(*last)
                sent += 1
                if sent == 1 or sent % 3000 == 0:
                    mlog("streaming ok (%d colors sent, last #%02x%02x%02x)" % ((sent,) + last))
            elif cmd == "restore":
                r, g, b = _parse(req.get("hex")) if req.get("hex") else last
                kb.set_led(MODE_CONSTANT, r, g, b)
                in_sync = False
                mlog("restored static color on quit")
        except Exception as e:
            mlog("write error (%s) - will reconnect" % e)
            kb.close()
            in_sync = False
            last_try = time.time()
            print(json.dumps({"ok": False, "event": "error", "error": str(e)}), flush=True)
    mlog("=== serve exit")
    kb.close()
    return 0


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 1
    cmd = args[0].lower()
    if cmd == "serve":
        return serve()
    if cmd == "diag":
        return diag()
    if cmd == "demo":
        # 10 s pink <-> cyan fade through the SAME live path RGB Studio uses
        # (Screen Sync mode + streamed colors), then back to static pink.
        import math
        kb = MonsGeekHE().open()
        try:
            kb.enter_sync()
            time.sleep(0.2)
            t0 = time.time()
            while time.time() - t0 < 10:
                f = (math.sin((time.time() - t0) * 2.0) + 1) / 2   # 0..1
                r = int(255 * (1 - f))
                g = int(212 * f)
                b = int(170 + 85 * f)
                kb.stream_color(r, g, b)
                time.sleep(0.033)
            kb.set_led(MODE_CONSTANT, 255, 0, 170)
            print("OK: demo done (back to static pink).")
        finally:
            kb.close()
        return 0
    kb = MonsGeekHE().open()
    try:
        if cmd == "probe":
            print("OK: found " + kb.name)
        elif cmd == "color":
            kb.enter_sync()
            kb.stream_color(*_parse(args[1] if len(args) > 1 else "ffffff"))
            print("OK: screen-sync mode, color sent.")
        elif cmd == "restore":
            kb.set_led(MODE_CONSTANT, *_parse(args[1] if len(args) > 1 else "ffffff"))
            print("OK: static color set on the keyboard.")
        else:
            print(__doc__)
            return 1
    finally:
        kb.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
