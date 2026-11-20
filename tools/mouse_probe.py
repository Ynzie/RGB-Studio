"""Logitech mouse probe (READ-ONLY): what the mouse supports, before RGB Studio
writes anything. Talks Logitech HID++ 2.0 through the receiver's vendor
interface (usage page 0xFF00). Results: diagnostics\\mouse_probe.txt

HID++ 2.0 long report: [0x11, device, feature-index, function<<4 | swid, 16 params]
Root (index 0) fn0 = look up a feature's index. Protocol facts per the public
HID++ documentation (as used by Solaar, libratbag and lowtech).
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "diagnostics", "mouse_probe.txt")
os.makedirs(os.path.dirname(OUT), exist_ok=True)
lines = []
SWID = 0x0B


def log(s=""):
    print(s)
    lines.append(str(s))


def save():
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


try:
    import hid
except ImportError:
    import subprocess
    subprocess.run([sys.executable, "-m", "pip", "install", "--user", "--quiet", "hidapi"])
    import site
    sys.path.append(site.getusersitepackages())
    import hid

FEATURES = {
    0x0000: "Root", 0x0001: "FeatureSet", 0x0003: "FirmwareInfo", 0x0005: "DeviceName",
    0x1000: "Battery", 0x1001: "BatteryVoltage", 0x1004: "UnifiedBattery", 0x1814: "ChangeHost",
    0x1B04: "ReprogControls", 0x1D4B: "WirelessStatus", 0x1F20: "ADC", 0x2201: "AdjustableDPI",
    0x2202: "ExtendedDPI", 0x2250: "Analysis", 0x8060: "ReportRate", 0x8061: "ExtendedReportRate",
    0x8070: "ColorLED", 0x8071: "RGBEffects", 0x8081: "PerKeyLighting", 0x8090: "ModeStatus",
    0x80A4: "AxisResponseCurve", 0x8100: "OnboardProfiles", 0x8110: "MouseButtonSpy",
    0x1802: "DeviceReset", 0x1803: "GPIO", 0x1806: "ConfigDeviceProps", 0x1891: "OTA",
    0x18A1: "LedTest", 0x1E00: "EnableHidden", 0x1EB0: "TdeAccess", 0x1E22: "SPI",
}


class Dev:
    def __init__(self, path, idx):
        self.h = hid.device()
        self.h.open_path(path)
        self.idx = idx

    def call(self, feat_index, fn, params=b"", timeout=1.0):
        msg = bytes([0x11, self.idx, feat_index, (fn << 4) | SWID]) + bytes(params)
        msg = msg + bytes(20 - len(msg))
        self.h.write(msg)
        end = time.time() + timeout
        while time.time() < end:
            r = self.h.read(64, 100)
            if not r:
                continue
            r = bytes(r)
            if r[0] not in (0x10, 0x11) or r[1] != self.idx:
                continue
            if r[2] == 0xFF and r[3] == feat_index:          # HID++ 2.0 error
                raise RuntimeError(f"error {r[5]:#x}")
            if r[2] == 0x8F:                                   # HID++ 1.0 error
                raise RuntimeError(f"hid++1 error {r[4]:#x}")
            if r[2] == feat_index and (r[3] & 0x0F) == SWID:
                return r[4:]
        raise TimeoutError("no reply")

    def feature(self, fid):
        r = self.call(0, 0, bytes([fid >> 8, fid & 255]))
        return r[0] or None


def main():
    log("Logitech mouse probe  " + time.strftime("%Y-%m-%d %H:%M:%S"))
    cands = [d for d in hid.enumerate(0x046D, 0)
             if d.get("usage_page") == 0xFF00 and d.get("usage") == 0x0002]
    for d in hid.enumerate(0x046D, 0):
        log(f"  046D:{d['product_id']:04X} {d.get('product_string')!r} usage {d.get('usage_page', 0):04X}:{d.get('usage', 0):04X}")
    if not cands:
        log("No Logitech HID++ interface found.")
        return
    dev = None
    for c in cands:
        for idx in (1, 2, 3, 4, 5, 6, 0xFF):
            try:
                d = Dev(c["path"], idx)
                proto = d.call(0, 1, b"\x00\x00\xAA")          # ping / protocol version
                log(f"\nFound device {idx} on 046D:{c['product_id']:04X}: HID++ {proto[0]}.{proto[1]}")
                dev = d
                break
            except Exception:
                pass
        if dev:
            break
    if not dev:
        log("\nThe receiver answered but no mouse did - move the mouse to wake it and run this again.")
        return

    i = dev.feature(0x0005)
    if i:
        n = dev.call(i, 0)[0]
        name = b""
        while len(name) < n:
            name += dev.call(i, 1, bytes([len(name)]))
        log("Name: " + name[:n].decode(errors="replace"))

    fs = dev.feature(0x0001)
    count = dev.call(fs, 0)[0]
    found = {}
    for k in range(1, count + 1):
        r = dev.call(fs, 1, bytes([k]))
        fid = (r[0] << 8) | r[1]
        found[fid] = k
    log("Features: " + ", ".join(f"{FEATURES.get(f, hex(f))}" for f in sorted(found)))

    if 0x2201 in found:
        i = found[0x2201]
        lst = dev.call(i, 1, b"\x00")
        vals = [(lst[k] << 8) | lst[k + 1] for k in range(1, 15, 2)]
        log("DPI list raw: " + lst.hex())
        cur = dev.call(i, 2, b"\x00")
        log(f"DPI now: {(cur[1] << 8) | cur[2]}  default: {(cur[3] << 8) | cur[4]}")
    if 0x8060 in found:
        i = found[0x8060]
        rl = dev.call(i, 0)[0]
        rates = [1000 // (b + 1) for b in range(8) if rl & (1 << b)]
        cur = dev.call(i, 1)[0]
        log(f"Polling rates: {rates} Hz   now: {1000 // cur if cur else '?'} Hz")
    if 0x8061 in found:
        i = found[0x8061]
        log("Extended report rate list raw: " + dev.call(i, 1).hex() + "  now raw: " + dev.call(i, 2).hex())
    if 0x8100 in found:
        i = found[0x8100]
        d = dev.call(i, 0)
        log("Onboard profiles description raw: " + d.hex())
        log(f"  memory model {d[0]}, profile format {d[1]}, macro format {d[2]}, profiles {d[3]}, "
            f"buttons {d[5]}, sectors {(d[6] << 8) | d[7]}, sector size {(d[8] << 8) | d[9]}")
        mode = dev.call(i, 2)[0]
        log(f"  mode: {mode} ({'onboard' if mode == 1 else 'host/software' if mode == 2 else '?'})")
        try:
            cp = dev.call(i, 4)
            log("  current profile raw: " + cp[:4].hex())
        except Exception as e:
            log(f"  current profile: {e}")
        size = (d[8] << 8) | d[9]
        for sector in (0x0000, 0x0001):
            data = b""
            off = 0
            try:
                while off < size:
                    o = min(off, size - 16)
                    chunk = dev.call(i, 5, bytes([sector >> 8, sector & 255, o >> 8, o & 255]))
                    data = data[:o] + chunk[:16]
                    off = o + 16
                log(f"  sector {sector:#06x} ({len(data)} bytes): {data.hex()}")
            except Exception as e:
                log(f"  sector {sector:#06x}: {e}")
    log("\ndone (nothing was changed)")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"ERROR: {e}")
    save()
    print(f"\nSaved to {OUT}. Tell Claude the mouse check is done.")
    print("This window closes in 15 seconds.")
    time.sleep(15)
