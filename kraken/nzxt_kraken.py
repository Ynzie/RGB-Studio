#!/usr/bin/env python3
"""
nzxt_kraken.py — direct USB driver for the NZXT Kraken Elite V2 (2024) LCD.

Ports the reverse-engineered protocol from cypherxdev77/nzxt-cam-linux (MIT) to
Windows via PyUSB + libusb. liquidctl only "partially" supports this model
(1e71:3012) and can't push the screen; this implements the exact bucket/bulk
sequence the firmware actually wants.

REQUIREMENTS
  - pip packages: pyusb, libusb-package, pillow   (all already installed with liquidctl)
  - BOTH Kraken USB interfaces bound to WinUSB (or libusbK) via Zadig:
        NZXT Kraken Elite V2 (Interface 0)  -> WinUSB   (bulk, image data)
        NZXT Kraken Elite V2 (Interface 1)  -> WinUSB   (interrupt, commands)
    (Converting Interface 1 removes the Kraken from OpenRGB/liquidctl, which is
     intended: this driver owns the whole cooler.)

USAGE
  python nzxt_kraken.py probe                 # list/verify the device + endpoints
  python nzxt_kraken.py image  <path.png>     # draw a static image on the LCD
  python nzxt_kraken.py gif    <path.gif>     # play a native GIF on the LCD
  python nzxt_kraken.py color  RRGGBB         # fill the LCD with a solid color
"""

import os
import sys
import time
import struct
import json
import base64
import io

VERSION = "2.8 (2026-11-20: live ring frames via 26 14, paced by the ack)"

# Debug log next to this file — survives app restarts so we can see exactly
# what the firmware answered even when the UI hides it. Rotated at 2 MB (it
# previously grew without bound: 66 MB, and got bundled into every build).
_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "driver.log")
_LOG_MAX = 2 * 1024 * 1024


def flog(msg):
    try:
        if os.path.exists(_LOG_PATH) and os.path.getsize(_LOG_PATH) > _LOG_MAX:
            os.replace(_LOG_PATH, _LOG_PATH + ".old")
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass

try:
    import usb.core
    import usb.util
    import libusb_package
except Exception as e:  # pragma: no cover
    msg = ("Python packages missing for the Kraken driver. Run: "
           "py -m pip install pyusb libusb-package pillow (%s)" % e)
    print(json.dumps({"ok": False, "event": "error", "error": msg}), flush=True)
    sys.exit(2)

# Commands (interface 1) go through Windows' normal HID driver when it owns
# that interface - the LeProta/NZXTKrakenLCDPlugin setup: HID is shared, so
# OpenRGB can drive the pump ring at the same time, and only interface 0
# (screen frames) needs WinUSB. Uses the 'hidapi' package; installed once
# automatically if missing. Without it, falls back to WinUSB on both.
def _load_hid():
    try:
        import hid
        return hid if hasattr(hid, "device") else None
    except Exception:
        pass
    try:
        import subprocess
        flog("hidapi missing - installing it once (pip install --user hidapi)")
        subprocess.run([sys.executable, "-m", "pip", "install", "--user", "--quiet", "hidapi"],
                       capture_output=True, timeout=180)
        import site, importlib
        try:
            sys.path.append(site.getusersitepackages())
        except Exception:
            pass
        importlib.invalidate_caches()
        import hid
        return hid if hasattr(hid, "device") else None
    except Exception as e:
        flog(f"hidapi unavailable: {e}")
        return None


_hidmod = _load_hid()

# ------------------------------------------------------------------ constants
NZXT_VID = 0x1E71
# 0x3013 = "Kraken Elite V2 B" revision (per LeProta/NZXTKrakenLCDPlugin).
KRAKEN_PIDS = (0x3008, 0x3009, 0x300C, 0x300E, 0x3012, 0x3013)
# Wedged-into-bootloader ID (documented by relative23/kraken-lcd): the cooler
# drops to this after too many LCD bucket uploads. Only a full PSU power-off
# (~30 s, a reboot is not enough) recovers it.
KRAKEN_BOOTLOADER_PID = 0x3011
STREAM_KIND_Q565 = 0x08   # Elite V2 bucketless frame stream format

PKT_SIZE   = 512                       # interrupt frames are always 512 bytes
BULK_CHUNK = 2 * 1024 * 1024           # 2 MiB bulk chunks
LCD_W, LCD_H = 640, 640                # Kraken Elite screen resolution
LCD_TOTAL_MEMORY = 24320               # KiB of LCD asset memory (liquidctl)

MAGIC = bytes([0x12, 0xFA, 0x01, 0xE8, 0xAB, 0xCD, 0xEF, 0x98, 0x76, 0x54, 0x32, 0x10])

BULK_OUT_EP = 0x02   # interface 0
INTR_OUT_EP = 0x01   # interface 1
INTR_IN_EP  = 0x81   # interface 1
IFACE_BULK      = 0
IFACE_INTERRUPT = 1


def bulk_info_rgba(size: int) -> bytes:
    return bytes([0x02, 0x00, 0x00, 0x00]) + struct.pack("<I", size)


def bulk_info_gif(size: int) -> bytes:
    return bytes([0x01, 0x00, 0x00, 0x00]) + struct.pack("<I", size)


# ------------------------------------------------------------------ Q565
# Compressed RGB565 frame format the Elite V2 accepts on the bucketless stream
# (stream kind 0x08). Format per LeProta/NZXTKrakenLCDPlugin q565.h (MIT):
#   b"q565" + width LE16 + height LE16, ops..., 0xFF
#   0xFE lo hi    full RGB565 pixel (also stored in the 64-slot color table)
#   0b00iiiiii    color-table slot (slot = (hi + lo) & 63)
#   0b11nnnnnn    run: repeat previous pixel n+1 times (n <= 61)
# Only ops whose decoder semantics are unambiguous are emitted (no DIFF/LUMA).
# Round-trip verified against a reference-semantics decoder.
_Q_R5 = bytes(((v * 249 + 1014) >> 11) for v in range(256))
_Q_G6 = bytes(((v * 253 + 505) >> 10) for v in range(256))


class StreamRejected(RuntimeError):
    """The cooler refused a bucketless stream start (caller may fall back)."""


def q565_encode(img):
    img = img.convert("RGB")
    w, h = img.size
    raw = img.tobytes()
    rs = raw[0::3].translate(_Q_R5)
    gs = raw[1::3].translate(_Q_G6)
    bs = raw[2::3].translate(_Q_R5)
    out = bytearray(b"q565")
    out += struct.pack("<HH", w, h)
    table = [None] * 64
    table[0] = 0x0000
    table[63] = 0xFFFF
    prev = None
    run = 0
    app = out.append
    for r, g, b in zip(rs, gs, bs):
        p = (r << 11) | (g << 5) | b
        if p == prev:
            run += 1
            if run == 62:
                app(0xC0 | 61)
                run = 0
            continue
        if run:
            app(0xC0 | (run - 1))
            run = 0
        slot = ((p >> 8) + (p & 0xFF)) & 63
        if table[slot] == p:
            app(slot)
        else:
            app(0xFE)
            app(p & 0xFF)
            app(p >> 8)
            table[slot] = p
        prev = p
    if run:
        app(0xC0 | (run - 1))
    app(0xFF)
    return bytes(out)


# ------------------------------------------------------------------ device I/O
class Kraken:
    def __init__(self, verbose=False):
        self.verbose = verbose
        self.dev = None
        self.hid = None           # HID handle for interface 1, when available
        self._hid_prefix = False  # True if writes need a leading report-ID 0
        self._ring_ready = False  # lighting handshake done on this link

    def log(self, *a):
        if self.verbose:
            print(*a, file=sys.stderr)

    def open(self):
        backend = libusb_package.get_libusb1_backend()
        dev = None
        for pid in KRAKEN_PIDS:
            dev = usb.core.find(idVendor=NZXT_VID, idProduct=pid, backend=backend)
            if dev is not None:
                break
        if dev is None:
            if usb.core.find(idVendor=NZXT_VID, idProduct=KRAKEN_BOOTLOADER_PID,
                             backend=backend) is not None:
                raise RuntimeError(
                    "Kraken is stuck in bootloader mode (USB 1e71:3011). Shut the PC "
                    "down, switch the PSU off (or unplug it) for 30 seconds, then boot. "
                    "A normal reboot does not recover it."
                )
            raise RuntimeError(
                "No NZXT Kraken found via libusb. Check that BOTH interfaces are "
                "bound to WinUSB/libusbK in Zadig, and NZXT CAM is not running."
            )
        self.dev = dev
        self.hid = self._open_hid()
        # On Windows/WinUSB the device is already configured; set_configuration
        # can raise 'Access denied'/'Busy' — that's fine, ignore it.
        try:
            dev.set_configuration()
        except Exception as e:
            self.log(f"(set_configuration skipped: {e})")
        # Claim the WinUSB interfaces: just the screen one when HID has commands.
        for i in ((IFACE_BULK,) if self.hid else (IFACE_BULK, IFACE_INTERRUPT)):
            try:
                usb.util.claim_interface(dev, i)
            except Exception as e:
                hint = ("Interface 1 is on the Windows HID driver but the 'hidapi' Python "
                        "package couldn't be loaded" if i == IFACE_INTERRUPT else
                        "In Zadig, bind 'NZXT Kraken Elite V2 (Interface 0)' to WinUSB")
                raise RuntimeError(f"Could not claim interface {i} ({e}). {hint}.")
        flog("Kraken opened: commands via " + ("HID (shared with OpenRGB)" if self.hid else "WinUSB"))
        return self

    def _open_hid(self):
        if _hidmod is None:
            return None
        try:
            for d in _hidmod.enumerate(NZXT_VID, 0):
                if d.get("product_id") in KRAKEN_PIDS and d.get("interface_number", -1) in (1, -1):
                    h = _hidmod.device()
                    h.open_path(d["path"])
                    h.set_nonblocking(False)
                    return h
        except Exception as e:
            flog(f"HID open failed ({e}) - using WinUSB for commands")
        return None

    def close(self):
        if self.hid is not None:
            try:
                self.hid.close()
            except Exception:
                pass
            self.hid = None
        if self.dev is not None:
            try:
                usb.util.dispose_resources(self.dev)
            except Exception:
                pass
            self.dev = None

    # ---- interrupt (commands / ACKs) -----------------------------------
    def write_cmd(self, data):
        if self.hid is not None:
            # hidapi pads to the report size itself. The first byte is the
            # command; if Windows wants a report-ID byte in front, add 0.
            payload = bytes(data) + bytes(max(0, 64 - len(data)))
            if not self._hid_prefix:
                try:
                    if self.hid.write(payload) >= 0:
                        return
                except Exception:
                    pass
                self._hid_prefix = True
                flog("HID: using report-ID prefix for writes")
            if self.hid.write(b"\x00" + payload) < 0:
                raise RuntimeError("HID write failed")
            return
        buf = bytearray(PKT_SIZE)
        buf[: len(data)] = bytes(data)
        self.dev.write(INTR_OUT_EP, bytes(buf), timeout=1000)

    def read_pkt(self, timeout_ms=800):
        if self.hid is not None:
            try:
                data = self.hid.read(PKT_SIZE, max(1, int(timeout_ms)))
                return bytes(data) if data else None
            except Exception:
                return None
        try:
            data = self.dev.read(INTR_IN_EP, PKT_SIZE, timeout=timeout_ms)
            return bytes(data)
        except usb.core.USBError:
            return None

    def drain(self, ms=250, per_read_ms=40):
        deadline = time.time() + ms / 1000.0
        while time.time() < deadline:
            if self.read_pkt(timeout_ms=per_read_ms) is None:
                break

    def read_until(self, prefix, attempts=25, per_read_ms=800):
        prefix = bytes(prefix)
        for _ in range(attempts):
            pkt = self.read_pkt(per_read_ms)
            if pkt and pkt[: len(prefix)] == prefix:
                return pkt
        self.log(f"TIMEOUT waiting for {prefix.hex()}")
        return None

    def read_until_time(self, prefix, seconds):
        """Like read_until, but bounded by wall time (status frames keep
        arriving, so an attempt count alone can stretch to 20 s)."""
        prefix = bytes(prefix)
        deadline = time.time() + seconds
        while time.time() < deadline:
            pkt = self.read_pkt(300)
            if pkt and pkt[: len(prefix)] == prefix:
                return pkt
        return None

    def write_read(self, data, expect):
        self.write_cmd(data)
        return self.read_until(expect)

    @staticmethod
    def ack_code(pkt):
        return pkt[14] if pkt and len(pkt) > 14 else 0xFF

    # ---- bulk (image data) ---------------------------------------------
    def bulk_write(self, data):
        self.dev.write(BULK_OUT_EP, bytes(data), timeout=5000)

    def bulk_write_all(self, data):
        sent = 0
        n = len(data)
        while sent < n:
            end = min(sent + BULK_CHUNK, n)
            self.bulk_write(data[sent:end])
            sent = end

    # ---- bucket management (ported from liquidctl KrakenZ3) ------------
    # The old code forced every upload to memory offset 0 with a single
    # bucket delete. A payload bigger than the previously stored one then
    # overlapped another bucket's memory, the firmware rejected the setup
    # (we never checked the ack), and the screen went BLACK. This is the
    # real memory allocator, same logic liquidctl/CAM use.

    def query_buckets(self):
        out = {}
        for b_i in range(16):
            self.write_cmd([0x30, 0x04, b_i])
            r = self.read_until([0x31, 0x04], attempts=8, per_read_ms=400)
            if r is not None:
                out[b_i] = r
        return out

    @staticmethod
    def find_unoccupied_bucket(buckets):
        for b_i, info in buckets.items():
            if not any(info[15:24]):
                return b_i
        return -1

    def delete_bucket(self, b_i):
        self.write_cmd([0x32, 0x02, b_i])
        r = self.read_until([0x33, 0x02], attempts=12, per_read_ms=500)
        return self.ack_code(r) == 0x01

    def prepare_bucket(self, b_i, bucket_filled):
        # A filled bucket must be deleted twice; a bucket that refuses
        # deletion means "try the next one".
        while True:
            if b_i >= 16:
                raise RuntimeError("no usable LCD bucket (all 16 refused deletion)")
            if not self.delete_bucket(b_i):
                b_i += 1
                bucket_filled = True
                continue
            if bucket_filled:
                bucket_filled = False
                continue
            return b_i

    @staticmethod
    def bucket_memory_offset(buckets, bucket_index, data_size):
        cur = buckets.get(bucket_index)
        if cur is None or len(cur) < 21:
            return [0x00, 0x00]
        cur_off = cur[17] | (cur[18] << 8)
        cur_size = cur[19] | (cur[20] << 8)
        if data_size <= cur_size:
            return [cur[17], cur[18]]           # fits where it already was
        min_occ, max_occ, overlap = cur_off, 0, False
        for b_i, b in buckets.items():
            if len(b) < 21:
                continue
            start = b[17] | (b[18] << 8)
            end = start + (b[19] | (b[20] << 8))
            max_occ = max(max_occ, end)
            min_occ = min(min_occ, start)
            if ((cur_off < start < cur_off + data_size)
                    or (start < cur_off and end > start)
                    or (start == cur_off and b_i != bucket_index)):
                overlap = True
        if not overlap:
            return [cur[17], cur[18]]           # can expand in place
        if max_occ + data_size < LCD_TOTAL_MEMORY:
            return [max_occ & 0xFF, (max_occ >> 8) & 0xFF]  # append at the end
        if data_size < min_occ:
            return [0x00, 0x00]                 # fits before the first bucket
        return None                             # memory exhausted -> reset all

    def delete_all_buckets(self):
        # Liquid mode releases the active bucket so everything is deletable.
        self.write_cmd([0x38, 0x01, 0x02, 0x00])
        self.read_until([0x39, 0x01], attempts=10, per_read_ms=500)
        for b_i in range(16):
            self.delete_bucket(b_i)

    def setup_bucket(self, start_b, end_b, mem_start, mem_size):
        r = self.write_read(
            [0x32, 0x01, start_b, end_b,
             mem_start[0], mem_start[1], mem_size[0], mem_size[1], 0x01],
            [0x33, 0x01],
        )
        return self.ack_code(r) == 0x01

    def switch_bucket(self, bucket, mode=0x04):
        self.write_cmd([0x38, 0x01, mode, bucket])
        r = self.read_until_time([0x39, 0x01], 3.0)
        return self.ack_code(r) == 0x01

    # ---- the actual image/GIF transfer (liquidctl _send_data port) -----
    def send_payload(self, payload: bytes, bulk_info: bytes):
        header = MAGIC + bulk_info
        data_size = (len(header) + len(payload) + 1023) // 1024
        if data_size >= LCD_TOTAL_MEMORY:
            raise RuntimeError(
                f"payload too large for LCD memory ({data_size} KiB > {LCD_TOTAL_MEMORY} KiB)"
            )
        dsz = [data_size & 0xFF, (data_size >> 8) & 0xFF]

        self.drain(300)
        time.sleep(0.05)

        # Wake the protocol (FW query).
        self.write_cmd([0x10, 0x01])
        r = self.read_until([0x11, 0x01], attempts=20)
        if r and len(r) >= 5:
            self.log(f"FW: {r[2]}.{r[3]}.{r[4]}")
        self.drain(200)
        time.sleep(0.05)

        # Init.
        self.write_read([0x36, 0x03], [0x37, 0x03])
        time.sleep(0.03)

        # Pick a bucket + a real memory offset.
        buckets = self.query_buckets()
        free = self.find_unoccupied_bucket(buckets)
        bucket = self.prepare_bucket(free if free != -1 else 0, free == -1)
        mem_start = self.bucket_memory_offset(buckets, bucket, data_size)
        if mem_start is None:
            self.log("LCD memory exhausted -> resetting all buckets")
            self.delete_all_buckets()
            bucket, mem_start = 0, [0x00, 0x00]
        self.log(f"Using bucket {bucket}, mem_start={mem_start}, dsz={dsz}")

        flog(f"send_payload: type={bulk_info[0]:#04x} size={len(payload)}B dsz={data_size}KiB "
             f"bucket={bucket} mem_start={mem_start}")
        if not self.setup_bucket(bucket, bucket + 1, mem_start, dsz):
            # One retry after a full reset, then give a real error instead of
            # a silent black screen.
            flog("send_payload: bucket setup REJECTED -> full reset + retry")
            self.delete_all_buckets()
            bucket, mem_start = 0, [0x00, 0x00]
            if not self.setup_bucket(bucket, bucket + 1, mem_start, dsz):
                flog("send_payload: bucket setup FAILED after reset")
                raise RuntimeError("LCD bucket setup failed (device rejected memory layout)")
        time.sleep(0.03)

        # Start transfer.
        self.write_cmd([0x36, 0x01, bucket])
        self.read_until([0x37, 0x01], attempts=20)
        time.sleep(0.03)

        # CRITICAL: header and image are SEPARATE bulk writes.
        self.bulk_write(header)
        self.bulk_write_all(payload)

        # End transfer. Big GIFs (near the 24 MB limit) take the cooler a
        # while to finish storing - wait for its "done" before activating.
        self.write_cmd([0x36, 0x02])
        done = self.read_until_time([0x37, 0x02], 4.0)
        if done is None:
            flog("send_payload: no end-of-transfer ack (continuing)")
        time.sleep(0.1)

        # Activate the bucket we just wrote — and verify it actually took.
        # A busy cooler can miss the first request: retry a few times before
        # giving up (re-uploading would be far riskier than re-asking).
        ok = False
        for attempt in range(4):
            self.drain(100)
            if self.switch_bucket(bucket):
                ok = True
                break
            flog(f"send_payload: activate attempt {attempt + 1} not acknowledged, retrying")
            time.sleep(1.0)
        if not ok:
            flog("send_payload: ACTIVATE failed")
            raise RuntimeError("The Kraken screen didn't accept the GIF/image (it may be too big - "
                               "try a smaller one, or pick it again)")
        time.sleep(0.1)
        # LCD traffic makes the lighting subsystem stop accepting bare ring
        # writes on this link — force the next streamed ring write to reopen.
        self._ring_ready = False
        flog("send_payload: OK")
        self.log("send_payload done")

    # ---- high-level helpers --------------------------------------------
    def send_rgba(self, rgba: bytes):
        self.send_payload(rgba, bulk_info_rgba(len(rgba)))

    def send_gif(self, gif_bytes: bytes):
        self.send_payload(gif_bytes, bulk_info_gif(len(gif_bytes)))

    # ---- bucketless frame streaming (LeProta/NZXTKrakenLCDPlugin path) --
    # For frequently-changing frames (sensor overlay). Unlike send_payload it
    # does NO bucket query/delete/setup/activate — each of those is a firmware
    # memory operation, and repeating them every few seconds is what wedges
    # the Elite V2 into its bootloader. Frames are Q565-compressed (~10 KB for
    # an overlay vs 1.6 MB raw), so a push is fast and light.
    def stream_frame(self, payload: bytes, kind: int = STREAM_KIND_Q565):
        self.drain(50)
        self.write_cmd([0x36, 0x01, 0x00, 0x01, kind])
        r = self.read_until([0x37, 0x01], attempts=25, per_read_ms=100)
        if self.ack_code(r) != 0x01:
            raise StreamRejected("stream start rejected (ack=%s)" %
                                 (None if r is None else self.ack_code(r)))
        header = MAGIC + bytes([kind, 0x00, 0x00, 0x00]) + struct.pack("<I", len(payload))
        self.bulk_write(header)           # header and data: separate bulk writes
        self.bulk_write_all(payload)
        self.write_cmd([0x36, 0x02])
        self.read_until([0x37, 0x02], attempts=25, per_read_ms=100)
        self._ring_ready = False          # LCD traffic: ring stream re-handshakes

    def set_ring(self, cmd_bytes: bytes):
        # Wake + init the command protocol first. On a freshly-opened link the
        # lighting subsystem ignores ring commands until the device has been
        # pinged/initialized (same handshake the LCD path does).
        self.drain(100)
        self.write_cmd([0x10, 0x01])
        r = self.read_until([0x11, 0x01], attempts=15, per_read_ms=400)
        if r is None:
            # Don't pretend it worked — raise so the app shows "Ring failed: ..."
            # instead of a silent no-op ("inputs not taking").
            raise RuntimeError(
                "Kraken didn't answer the wake handshake (0x10). USB link is "
                "wedged or another process holds the device — check for a "
                "leftover RGB Studio.exe / python.exe in Task Manager."
            )
        self.write_cmd([0x36, 0x03])
        self.read_until([0x37, 0x03], attempts=15, per_read_ms=400)
        flog(f"set_ring: sending {bytes(cmd_bytes[:8]).hex()}... (60B total)")
        self.write_cmd(cmd_bytes)
        time.sleep(0.05)
        self._ring_ready = True
        flog("set_ring: sent")

    def set_ring_fast(self, cmd_bytes: bytes):
        # Streaming path for software-driven ring colors: handshake once per
        # link, then drained writes. The DRAIN is load-bearing: the Kraken
        # pushes 0x75 status frames continuously, and if the host never reads
        # them the firmware stops processing new commands (this is why bare
        # writes were silently ignored — nzxt-cam-linux works because its
        # poller thread reads the IN endpoint nonstop).
        if not self._ring_ready:
            self.set_ring(cmd_bytes)
            return
        self.drain(40)
        self.write_cmd(cmd_bytes)

    # ---- per-LED "Direct" lighting (HUE2 0x22 family) -------------------
    # Lets the app stream every LED from its own effects clock, so the ring
    # stays in lockstep with every other device (the firmware animations run
    # on their own clock and speed). Framing per OpenKraken's PROTOCOL.md,
    # verified on a Kraken 2024 Elite RGB (1e71:3012):
    #   0x22 0x10 mask 00 + LEDs 0-19 (GRB)   0x22 0x11 mask 00 + LEDs 20-39
    #   0x22 0xA0 mask ... apply (OpenRGB's byte set)
    # mask 0x01 = pump ring (24 LEDs), 0x02 = the Kraken's RGB fan port.
    def lighting_handshake(self):
        self.drain(100)
        self.write_cmd([0x10, 0x01])
        if self.read_until([0x11, 0x01], attempts=15, per_read_ms=400) is None:
            raise RuntimeError("Kraken didn't answer the wake handshake (0x10).")
        self.write_cmd([0x36, 0x03])
        self.read_until([0x37, 0x03], attempts=15, per_read_ms=400)
        self._ring_ready = True

    def query_lighting_info(self):
        """{channel_index: led_count} from the 0x20 0x03 accessory report, or None."""
        self.drain(60)
        self.write_cmd([0x20, 0x03])
        r = self.read_until([0x21, 0x03], attempts=15, per_read_ms=300)
        if r is None or len(r) < 27:
            return None
        out = {}
        for ch in range(min(r[14], 2)):
            total = 0
            for slot in range(6):
                acc = r[15 + ch * 6 + slot]
                if acc:
                    total += _ACCESSORY_LEDS.get(acc, 8)
            out[ch] = min(total, 40)
        flog(f"lighting info: channels={r[14]} raw={bytes(r[14:27]).hex()} leds={out}")
        return out

    # ---- the live write (what NZXT CAM uses for host-drawn frames) --------
    # Per ushineko/sanshoku docs/kraken-lighting.md (from a capture of CAM 4.76
    # on this exact cooler, firmware 1.2.12): `26 14 <ch> <show>` + every LED
    # in G,R,B, in ONE full 512-byte report. It draws over the ring without
    # storing anything, and the cooler acknowledges it in ~2 ms. Waiting for
    # that acknowledgement before the next frame paces a stream without loss
    # (tested to 60 fps); not waiting makes the firmware drop frames. The
    # `22 xx` family above is acknowledged only after 335-450 ms - that was
    # the ~2 updates/s wall.
    def write_live(self, ch, leds, show=None):
        payload = bytearray()
        for (cr, cg, cb) in leds[:40]:
            payload += bytes((cg & 255, cr & 255, cb & 255))
        pkt = bytes([0x26, 0x14, ch, ch if show is None else show]) + bytes(payload)
        self.write_cmd(pkt + bytes(max(0, 512 - len(pkt))))
        # Pace on the cooler's reply to this frame (its first byte is 0x27 by
        # NZXT's request/reply pattern; any reply within the window counts as
        # "ready" so an unexpected prefix can't stall the stream).
        end = time.time() + 0.05
        while time.time() < end:
            p = self.read_pkt(10)
            if p is not None and p[0] == 0x27:
                return bytes(p[:2]).hex()
        return None

    def write_leds(self, mask, leds):
        payload = bytearray()
        for (cr, cg, cb) in leds[:40]:
            # GRB on the wire. No gamma here: the app's frames are already the
            # same values every other device gets, so the ring matches them
            # (gamma also crushed fades into a few dark steps = choppy).
            payload += bytes((cg & 255, cr & 255, cb & 255))
        self.write_cmd(bytes([0x22, 0x10, mask, 0x00]) + bytes(payload[:60]))
        if len(leds) > 20:
            self.write_cmd(bytes([0x22, 0x11, mask, 0x00]) + bytes(payload[60:120]))
        self.write_cmd(bytes([0x22, 0xA0, mask, 0x00, 0x01, 0x00, 0x00, 0x28,
                              0x00, 0x00, 0x80, 0x00, 0x32, 0x00, 0x00, 0x01]))

    # ---- LCD panel settings (liquidctl KrakenZ3 set_screen port) -------
    def get_lcd_info(self):
        self.drain(50)
        self.write_cmd([0x30, 0x01])
        r = self.read_until([0x31, 0x01], attempts=10, per_read_ms=500)
        if r is None:
            raise RuntimeError("Kraken didn't answer the LCD info query (0x30 0x01)")
        return {"brightness": r[0x18], "orientation": r[0x1A] * 90}

    def set_brightness(self, pct):
        pct = max(0, min(100, int(pct)))
        info = self.get_lcd_info()   # command must carry the CURRENT orientation
        self.write_cmd([0x30, 0x02, 0x01, pct, 0x00, 0x00, 0x01, info["orientation"] // 90])
        time.sleep(0.05)

    def set_orientation(self, degrees):
        degrees = int(degrees)
        if degrees not in (0, 90, 180, 270):
            raise ValueError("orientation must be 0, 90, 180 or 270")
        info = self.get_lcd_info()   # command must carry the CURRENT brightness
        self.write_cmd([0x30, 0x02, 0x01, info["brightness"], 0x00, 0x00, 0x01, degrees // 90])
        time.sleep(0.05)

    def get_status(self, timeout_s=2.0):
        # The Kraken pushes 0x75 status frames on the interrupt-IN endpoint.
        self.drain(50)
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            pkt = self.read_pkt(300)
            if pkt and len(pkt) >= 20 and pkt[0] == 0x75:
                liquid = pkt[15] + pkt[16] / 10.0
                pump = pkt[17] | (pkt[18] << 8)
                if 1.0 <= liquid <= 59.0:
                    return {"liquid": round(liquid, 1), "pump": pump}
        return None


# ------------------------------------------------------------------ image prep
def image_to_rgba(path):
    from PIL import Image
    img = Image.open(path).convert("RGBA").resize((LCD_W, LCD_H))
    raw = bytearray(img.tobytes())      # RGBA, row-major
    # Firmware requires alpha == 0x00 on every pixel, else colors are mangled.
    for i in range(3, len(raw), 4):
        raw[i] = 0x00
    return bytes(raw)


def solid_rgba(r, g, b):
    px = bytes([r, g, b, 0x00])
    return px * (LCD_W * LCD_H)


def _encode_gif(frames, durations):
    out = io.BytesIO()
    frames[0].save(
        out, format="GIF", save_all=True, append_images=frames[1:],
        duration=durations, loop=0, disposal=1, optimize=False,
    )
    return out.getvalue()


def resize_gif(path):
    # The Kraken firmware only renders a GIF already sized to the 640x640 screen.
    from PIL import Image, ImageSequence
    with open(path, "rb") as f:
        raw = f.read()
    src = Image.open(io.BytesIO(raw))
    max_bytes = (LCD_TOTAL_MEMORY - 64) * 1024

    # Pass-through: already screen-sized and fits -> send the ORIGINAL bytes.
    # The source's delta-encoded frames stay intact, which is both the smallest
    # file and the smoothest firmware playback (full re-encode of every frame
    # was producing 20+ MB monsters the firmware decoded too slowly = choppy).
    if src.size == (LCD_W, LCD_H) and len(raw) <= max_bytes:
        return raw

    frames, durations = [], []
    for fr in ImageSequence.Iterator(src):
        frames.append(fr.convert("RGB").resize((LCD_W, LCD_H)))
        durations.append(int(fr.info.get("duration", 100)))
    if not frames:
        raise RuntimeError("GIF has no frames")

    # Quantize every frame to ONE shared palette. With a common palette PIL
    # stores only each frame's changed region (delta encoding) instead of a
    # full 640x640 image per frame -> far smaller file, smooth decode.
    base = frames[0].quantize(colors=256)
    try:
        dither = Image.Dither.NONE   # dithering per-frame is slow; skip it
    except AttributeError:            # older Pillow
        dither = 0
    frames = [f.quantize(palette=base, dither=dither) for f in frames]
    # convert("RGB") turns a palette-index transparency into an RGB TUPLE in
    # frame.info; the GIF encoder then chokes on `transparency & 0xFF`. The
    # frames are fully opaque at this point anyway — drop the stale info.
    for f in frames:
        f.info.pop("transparency", None)
    data = _encode_gif(frames, durations)
    # The LCD has 24320 KiB of asset memory TOTAL. A GIF that doesn't fit is
    # stored truncated and displays as a black screen. Halve the frame count
    # (doubling per-frame duration to keep the same playback speed) until it
    # fits, and error out loudly if it still can't.
    max_bytes = (LCD_TOTAL_MEMORY - 64) * 1024
    while len(data) > max_bytes and len(frames) > 1:
        frames = frames[::2]
        durations = [d * 2 for d in durations[::2]]
        data = _encode_gif(frames, durations)
    if len(data) > max_bytes:
        raise RuntimeError(
            f"GIF is too large for the LCD's 24 MB memory even after frame reduction "
            f"({len(data) // 1024} KiB) — try a shorter or smaller GIF"
        )
    return data


# ------------------------------------------------------------------ ring LEDs
# Full liquidctl KrakenX3/Z3 lighting protocol (kraken3.py _write_colors).
# Colors on the wire are GRB + gamma^2.2 so LEDs match the UI swatch.
_SPEED_VALUE = {
    0:  [[0x32, 0x00]] * 5,
    1:  [[0x50, 0x00], [0x3C, 0x00], [0x28, 0x00], [0x14, 0x00], [0x0A, 0x00]],
    2:  [[0x5E, 0x01], [0x2C, 0x01], [0xFA, 0x00], [0x96, 0x00], [0x50, 0x00]],
    3:  [[0x40, 0x06], [0x14, 0x05], [0xE8, 0x03], [0x20, 0x03], [0x58, 0x02]],
    4:  [[0x20, 0x03], [0xBC, 0x02], [0xF4, 0x01], [0x90, 0x01], [0x2C, 0x01]],
    5:  [[0x19, 0x00], [0x14, 0x00], [0x0F, 0x00], [0x07, 0x00], [0x04, 0x00]],
    6:  [[0x28, 0x00], [0x1E, 0x00], [0x14, 0x00], [0x0A, 0x00], [0x04, 0x00]],
    7:  [[0x32, 0x00], [0x28, 0x00], [0x1E, 0x00], [0x14, 0x00], [0x0A, 0x00]],
    8:  [[0x14, 0x00]] * 5,
    9:  [[0x00, 0x00]] * 5,
    10: [[0x37, 0x00], [0x28, 0x00], [0x19, 0x00], [0x0A, 0x00], [0x00, 0x00]],
}

# name -> (mval, size_variant, speed_scale, mincolors, maxcolors)
_RING_MODES = {
    "off":              (0x00, 0x00, 0, 0, 0),
    "fixed":            (0x00, 0x00, 0, 1, 1),
    "fading":           (0x01, 0x00, 1, 1, 8),
    "spectrum-wave":    (0x02, 0x00, 2, 0, 0),
    "marquee-3":        (0x03, 0x03, 2, 1, 1),
    "marquee-4":        (0x03, 0x04, 2, 1, 1),
    "marquee-5":        (0x03, 0x05, 2, 1, 1),
    "marquee-6":        (0x03, 0x06, 2, 1, 1),
    "covering-marquee": (0x04, 0x00, 2, 1, 8),
    "alternating-3":    (0x05, 0x03, 3, 1, 2),
    "pulse":            (0x06, 0x00, 5, 1, 8),
    "breathing":        (0x07, 0x00, 6, 1, 8),
    "candle":           (0x08, 0x00, 0, 1, 1),
    "starry-night":     (0x09, 0x00, 5, 1, 1),
    "rainbow-flow":     (0x0B, 0x00, 2, 0, 0),
    "super-rainbow":    (0x0C, 0x00, 2, 0, 0),
    "rainbow-pulse":    (0x0D, 0x00, 2, 0, 0),
    "tai-chi":          (0x0E, 0x00, 7, 2, 2),
    "water-cooler":     (0x0F, 0x00, 6, 2, 2),
    "loading":          (0x10, 0x00, 8, 1, 1),
    # aliases so old saved settings / CLI habits keep working
    "spectrum":         (0x02, 0x00, 2, 0, 0),
    "rainbow":          (0x0B, 0x00, 2, 0, 0),
    "marquee":          (0x03, 0x03, 2, 1, 1),
}


# HUE2 accessory id -> LED count (OpenRGB table; 0x1E/0x1B confirmed on a
# Kraken 2024 Elite by OpenKraken). Unknown ids count as 8.
_ACCESSORY_LEDS = {0x1E: 24, 0x17: 8, 0x18: 8, 0x1B: 24, 0x1D: 24}


def _gamma(v):
    return int(round((v / 255.0) ** 2.2 * 255))


# Static-timing value per channel (liquidctl _STATIC_VALUE). 0x07 = all channels.
_RING_STATIC = {0x01: 40, 0x02: 8, 0x04: 1, 0x07: 40}


def build_ring_cmd(mode, colors=None, speed_idx=2, channel=0x07):
    # NOTE: on Kraken Elite V2 the ring is NOT channel 0x02 (that's X3). We target
    # channel 0x07 = "all channels at once" by default — CONFIRMED working on this
    # rig. colors is a list of (r, g, b) tuples.
    mode = (mode or "fixed").lower()
    if mode not in _RING_MODES:
        raise ValueError(f"unknown ring mode '{mode}' ({', '.join(sorted(_RING_MODES))})")
    mval, size_variant, speed_scale, mincolors, maxcolors = _RING_MODES[mode]
    cid = channel
    static_val = _RING_STATIC.get(channel, 40)

    colors = [tuple(c) for c in (colors or [])]
    if mode == "off" or maxcolors == 0:
        colors = [(0, 0, 0)]              # liquidctl sends one placeholder color
    else:
        if not colors:
            colors = [(255, 255, 255)]
        colors = colors[:maxcolors]
        while len(colors) < mincolors:    # e.g. tai-chi/water-cooler need 2
            colors.append(colors[-1])

    spd = _SPEED_VALUE.get(speed_scale, _SPEED_VALUE[0])[min(max(speed_idx, 0), 4)]

    # backward/direction byte (liquidctl)
    if "marquee" in mode:
        backward = 0x04
    elif mode == "starry-night":
        backward = 0x01
    else:
        backward = 0x00

    # mode-related byte (liquidctl)
    if mode in ("fading", "pulse", "breathing"):
        mode_rel = 0x08
    elif mode in ("tai-chi", "water-cooler"):
        mode_rel = 0x05
    elif mode == "loading":
        mode_rel = 0x04
    else:
        mode_rel = 0x00

    color_count = 0x01 if mode == "water-cooler" else len(colors)
    led_size = size_variant if mval in (0x03, 0x05) else 0x03

    cmd = bytearray([0x2A, 0x04, cid, cid, mval, spd[0], spd[1]])
    color_bytes = bytearray(48)
    for i, (cr, cg, cb) in enumerate(colors[:16]):
        color_bytes[i * 3]     = _gamma(cg)   # GRB order
        color_bytes[i * 3 + 1] = _gamma(cr)
        color_bytes[i * 3 + 2] = _gamma(cb)
    cmd += color_bytes
    cmd += bytes([backward, color_count, mode_rel, static_val, led_size])
    return bytes(cmd)


# ------------------------------------------------------------------ serve mode
def _png_b64_to_rgba(b64):
    return _image_to_rgba_bytes(_png_b64_to_image(b64))


def _png_b64_to_image(b64):
    from PIL import Image
    img = Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
    if img.size != (LCD_W, LCD_H):
        img = img.resize((LCD_W, LCD_H))
    return img


def _image_to_rgba_bytes(img):
    # Firmware needs alpha == 0x00 on every pixel. Slice assignment does it in
    # C instead of a 409,600-iteration Python loop.
    raw = bytearray(img.convert("RGBA").tobytes())
    raw[3::4] = bytes(len(raw) // 4)
    return bytes(raw)


def _parse_color(s):
    h = (s or "").lstrip("#")
    if len(h) < 6:
        return 0, 0, 0
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _ring_colors_from_req(req):
    cols = req.get("colors")
    if cols:
        return [_parse_color(c) for c in cols]
    if req.get("color"):
        return [_parse_color(req["color"])]
    return []


def serve():
    """Persistent stdin loop: one JSON command per line, USB stays open.
    Lets RGB Studio stream frames without re-spawning Python each time.
    Commands: {"cmd":"image","path":...} {"cmd":"gif","path":...}
              {"cmd":"frame","png":"<base64>"} {"cmd":"color","hex":"RRGGBB"}
              {"cmd":"ring","mode":...,"colors":["RRGGBB",...],"speed":2}
              {"cmd":"brightness","value":0-100} {"cmd":"orientation","value":0|90|180|270}
              {"cmd":"lcdinfo"} {"cmd":"status"} {"cmd":"quit"}
    """
    flog(f"=== serve start: {VERSION} ({os.path.abspath(__file__)})")
    k = Kraken(verbose=False)
    try:
        k.open()
    except Exception as e:
        # Report cleanly (the app shows this text) instead of a traceback.
        flog(f"serve: open failed: {e}")
        print(json.dumps({"ok": False, "event": "error", "error": str(e)}), flush=True)
        return 3
    print(json.dumps({"ok": True, "event": "ready", "script": __file__, "version": VERSION}), flush=True)
    stream_ok = True          # cooler accepts bucketless Q565 streaming
    last_bucket_frame = 0.0   # time of last fallback (bucket) frame upload
    BUCKET_FRAME_MIN_S = 30   # fallback path: at most 1 upload per 30 s (wedge risk)
    led_fails = 0             # consecutive streamed-lighting failures
    led_stats = {"n": 0, "busy": 0.0, "worst": 0.0, "t0": time.time(), "last": None, "gap_max": 0.0}
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        cmd = None
        try:
            req = json.loads(line)
            cmd = req.get("cmd")
            if cmd not in ("frame", "status", "ring_direct", "leds"):  # too chatty to log
                flog(f"serve cmd: {line[:200]}")
            # If a previous reopen failed, retry before the next command
            # instead of leaving the whole session wedged.
            if k.dev is None and cmd != "quit":
                k.open()
            if cmd == "image":
                k.send_rgba(image_to_rgba(req["path"]))
            elif cmd == "frame":
                # Overlay frames change every few seconds: stream them
                # bucketless (no firmware memory ops). If this cooler refuses
                # the stream, fall back to bucket uploads, rate-limited hard,
                # because frequent bucket uploads can wedge the cooler.
                img = _png_b64_to_image(req["png"])
                sent = False
                if stream_ok:
                    try:
                        k.stream_frame(q565_encode(img))
                        sent = True
                    except StreamRejected as e:
                        stream_ok = False
                        flog(f"frame: bucketless stream unsupported here, using fallback: {e}")
                if not sent:
                    if time.time() - last_bucket_frame < BUCKET_FRAME_MIN_S:
                        print(json.dumps({"ok": True, "cmd": cmd, "skipped": "rate-limited"}), flush=True)
                        continue
                    k.send_rgba(_image_to_rgba_bytes(img))
                    last_bucket_frame = time.time()
            elif cmd == "color":
                r, g, b = _parse_color(req.get("hex"))
                k.send_rgba(solid_rgba(r, g, b))
            elif cmd == "gif":
                k.send_gif(resize_gif(req["path"]))
            elif cmd == "ring":
                cmd_bytes = build_ring_cmd(
                    req.get("mode", "fixed"),
                    _ring_colors_from_req(req),
                    int(req.get("speed", 2)),
                    int(req.get("channel", 0x07)),
                )
                # Ring writes only reliably take on a freshly-opened USB link —
                # the CLI (which always opens fresh) works, the long-lived link
                # doesn't. So replicate the CLI exactly: reopen, then send.
                k.close()
                time.sleep(0.2)
                k = Kraken(verbose=False)
                k.open()
                k.set_ring(cmd_bytes)
            elif cmd == "ring_direct":
                # Streaming ring color: used by the effects engine to keep the
                # ring on the same clock as every other device. Ring writes
                # only take on a freshly-opened link, so reopen ONCE when the
                # stream starts (or after LCD traffic), then bare writes.
                cmd_bytes = build_ring_cmd(
                    req.get("mode", "fixed"),
                    _ring_colors_from_req(req),
                    int(req.get("speed", 2)),
                    int(req.get("channel", 0x07)),
                )
                if not k._ring_ready:
                    k.close()
                    time.sleep(0.2)
                    k = Kraken(verbose=False)
                    k.open()
                    k.set_ring(cmd_bytes)   # handshake + write; sets _ring_ready
                else:
                    k.set_ring_fast(cmd_bytes)
            elif cmd == "leds":
                # One per-LED frame from the app's effects engine: ring (mask
                # 0x01) and, if anything is plugged in, the fan port (0x02).
                f_start = time.time()
                if req.get("reinit"):
                    k._ring_ready = False        # e.g. after sleep: handshake again
                if not k._ring_ready:
                    k.lighting_handshake()
                ring = [_parse_color(c) for c in (req.get("ring") or [])]
                fans = [_parse_color(c) for c in (req.get("fans") or [])]
                ack = None
                if ring:
                    ack = k.write_live(0x01, ring)
                if fans:
                    k.write_live(0x02, fans)
                if led_stats.get("probe", 0) < 3:
                    led_stats["probe"] = led_stats.get("probe", 0) + 1
                    flog(f"live frame {led_stats['probe']}: cooler reply {ack or 'none within 50 ms'}")
                led_fails = 0
                reps = led_stats.setdefault("replies", {})
                reps[ack or "no-ack"] = reps.get(ack or "no-ack", 0) + 1
                # Every 10 s: how many frames/s actually went out, and how long
                # each took here (tells host-side slowness from device limits).
                f_end = time.time()
                st = led_stats
                st["n"] += 1
                st["busy"] += f_end - f_start
                st["worst"] = max(st["worst"], f_end - f_start)
                if st["last"] is not None:
                    st["gap_max"] = max(st["gap_max"], f_start - st["last"])
                st["last"] = f_start
                if f_end - st["t0"] >= 10:
                    span = f_end - st["t0"]
                    flog(f"leds: {st['n'] / span:.1f} frames/s, handling avg "
                         f"{1000 * st['busy'] / max(1, st['n']):.1f} ms, worst {1000 * st['worst']:.0f} ms, "
                         f"longest gap {1000 * st['gap_max']:.0f} ms, replies {st.get('replies') or 'none'}")
                    st.update(n=0, busy=0.0, worst=0.0, t0=f_end, gap_max=0.0, replies={})
            elif cmd == "lightinfo":
                info = k.query_lighting_info() or {}
                print(json.dumps({"ok": True, "info": {"ring": info.get(0) or 24,
                                                       "fans": info.get(1) or 0}}), flush=True)
                continue
            elif cmd == "brightness":
                k.set_brightness(req.get("value", 80))
            elif cmd == "orientation":
                k.set_orientation(req.get("value", 0))
            elif cmd == "lcdinfo":
                print(json.dumps({"ok": True, "info": k.get_lcd_info()}), flush=True)
                continue
            elif cmd == "status":
                print(json.dumps({"ok": True, "status": k.get_status()}), flush=True)
                continue
            elif cmd == "quit":
                break
            else:
                print(json.dumps({"ok": False, "error": f"unknown cmd {cmd}"}), flush=True)
                continue
            print(json.dumps({"ok": True, "cmd": cmd}), flush=True)
        except Exception as e:
            flog(f"serve ERROR ({cmd}): {e}")
            if cmd in ("leds", "ring_direct"):
                # Streamed lighting failed: handshake again next frame; after a
                # few in a row (replug / resume) reopen the USB link.
                k._ring_ready = False
                led_fails += 1
                if led_fails >= 3:
                    k.close()
                    led_fails = 0
            print(json.dumps({"ok": False, "error": str(e)}), flush=True)
    k.close()
    flog("=== serve exit")
    return 0


# ------------------------------------------------------------------ CLI
def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 1
    cmd = args[0].lower()
    k = Kraken(verbose=True)

    if cmd == "probe":
        k.open()
        print("OK: Kraken opened and both interfaces claimed.")
        k.close()
        return 0

    if cmd == "image":
        if len(args) < 2:
            print("usage: image <path>")
            return 1
        rgba = image_to_rgba(args[1])
        k.open(); k.send_rgba(rgba); k.close()
        print("OK: image sent.")
        return 0

    if cmd == "gif":
        if len(args) < 2:
            print("usage: gif <path>")
            return 1
        data = resize_gif(args[1])
        k.open(); k.send_gif(data); k.close()
        print("OK: gif sent.")
        return 0

    if cmd == "color":
        if len(args) < 2:
            print("usage: color RRGGBB")
            return 1
        r, g, b = _parse_color(args[1])
        k.open(); k.send_rgba(solid_rgba(r, g, b)); k.close()
        print("OK: color sent.")
        return 0

    if cmd == "ring":
        # ring <mode> [RRGGBB[,RRGGBB...]] [speed 0-4] [channel-hex e.g. 07|01|02|04]
        if len(args) < 2:
            print("usage: ring <mode> [RRGGBB[,RRGGBB...]] [speed] [channel]")
            print("modes: " + ", ".join(sorted(_RING_MODES)))
            return 1
        mode = args[1]
        colors = [_parse_color(c) for c in args[2].split(",")] if len(args) > 2 else []
        speed = int(args[3]) if len(args) > 3 else 2
        chan = int(args[4], 16) if len(args) > 4 else 0x07
        k.open(); k.set_ring(build_ring_cmd(mode, colors, speed, chan)); k.close()
        print(f"OK: ring {mode} sent (channel 0x{chan:02x}).")
        return 0

    if cmd == "brightness":
        if len(args) < 2:
            print("usage: brightness <0-100>")
            return 1
        k.open(); k.set_brightness(int(args[1])); k.close()
        print("OK: brightness set.")
        return 0

    if cmd == "orientation":
        if len(args) < 2:
            print("usage: orientation <0|90|180|270>")
            return 1
        k.open(); k.set_orientation(int(args[1])); k.close()
        print("OK: orientation set.")
        return 0

    if cmd == "lcdinfo":
        k.open(); info = k.get_lcd_info(); k.close()
        print(json.dumps(info))
        return 0

    if cmd == "status":
        k.open(); st = k.get_status(); k.close()
        print(json.dumps(st) if st else "null")
        return 0

    if cmd == "serve":
        return serve()

    print(f"unknown command: {cmd}")
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
