"""Kraken pump-ring test: tries every known way of setting the ring, one at a
time, and asks what you saw. Answers + the cooler's replies are saved to
diagnostics\\ring_test.txt so the app can use whatever actually works.
Run via "Test Kraken Ring.bat" (it closes RGB Studio first)."""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "kraken"))
import nzxt_kraken as nk  # noqa: E402

OUT = os.path.join(ROOT, "diagnostics", "ring_test.txt")
os.makedirs(os.path.dirname(OUT), exist_ok=True)
lines = []


def log(s):
    print(s)
    lines.append(s)


def replies(k, ms=300):
    seen = []
    end = time.time() + ms / 1000
    while time.time() < end:
        p = k.read_pkt(60)
        if p is None:
            continue
        key = bytes(p[:2]).hex()
        if key not in ("7502",):  # skip the regular status frames
            seen.append(bytes(p[:4]).hex())
    return seen or ["none"]


def ask(q):
    while True:
        a = input(f"\n  >> {q} [y/n]: ").strip().lower()
        if a in ("y", "n"):
            return a


def fixed(k, rgb, channel):
    k.write_cmd(nk.build_ring_cmd("fixed", [rgb], 2, channel))


def direct(k, colors, apply_byte=0x28, apply=True):
    payload = bytearray()
    for (r, g, b) in colors:
        payload += bytes((g, r, b))
    k.write_cmd(bytes([0x22, 0x10, 0x01, 0x00]) + bytes(payload[:60]))
    k.write_cmd(bytes([0x22, 0x11, 0x01, 0x00]) + bytes(payload[60:120]))
    if apply:
        k.write_cmd(bytes([0x22, 0xA0, 0x01, 0x00, 0x01, 0x00, 0x00, apply_byte,
                           0x00, 0x00, 0x80, 0x00, 0x32, 0x00, 0x00, 0x01]))


def hsv(h):
    import colorsys
    r, g, b = colorsys.hsv_to_rgb((h % 360) / 360, 1, 1)
    return (int(r * 255), int(g * 255), int(b * 255))


def main():
    log("Kraken ring test  " + time.strftime("%Y-%m-%d %H:%M:%S"))
    k = nk.Kraken()
    k.open()
    log("connection: " + ("HID (shared)" if k.hid else "WinUSB"))
    k.lighting_handshake()
    print("\nWatch the PUMP RING (the light around the screen) and answer each question.")

    steps = [
        ("A", "old method, all channels (0x2A, ch 0x07): ring RED?",
         lambda: fixed(k, (255, 0, 0), 0x07)),
        ("B", "old method, ring channel (0x2A, ch 0x01): ring GREEN?",
         lambda: fixed(k, (0, 255, 0), 0x01)),
        ("C", "per-LED method (0x22, apply 0x28): ring BLUE?",
         lambda: direct(k, [(0, 0, 255)] * 24, 0x28)),
        ("D", "per-LED method (0x22, apply 0x08): ring YELLOW?",
         lambda: direct(k, [(255, 200, 0)] * 24, 0x08)),
        ("E", "per-LED, no apply packet: ring PURPLE?",
         lambda: direct(k, [(160, 0, 255)] * 24, apply=False)),
    ]
    for tag, q, fn in steps:
        k.drain(100)
        fn()
        r = replies(k)
        a = ask(q)
        log(f"{tag}: {q} -> {a}   replies: {r}")

    # Animation tests: does the ring keep updating, and how fast?
    for fps in (2, 5, 10):
        print(f"\nStep F{fps}: per-LED rainbow spinning at {fps} updates/s for 6 seconds - watch it...")
        t0 = time.time()
        rej = 0
        n = 0
        while time.time() - t0 < 6:
            off = (time.time() - t0) * 120
            k.drain(10, per_read_ms=5)
            direct(k, [hsv(off + i * 15) for i in range(24)], 0x28)
            n += 1
            rr = replies(k, 20)
            rej += sum(1 for x in rr if x.startswith("ff01"))
            time.sleep(max(0, 1 / fps - 0.03))
        a = input("\n  >> Did it SPIN smoothly (s), STEP/jump (j), or stay FROZEN (f)? ").strip().lower()
        log(f"F{fps}: per-LED spin at {fps}/s -> {a}   frames {n}, ff01 replies {rej}")

    for fps in (5, 10):
        print(f"\nStep G{fps}: old method fading cyan <-> pink at {fps} updates/s for 6 seconds - watch it...")
        t0 = time.time()
        while time.time() - t0 < 6:
            x = (1 + __import__('math').sin((time.time() - t0) * 2)) / 2
            c = (int(255 * x), int(225 * (1 - x) + 92 * x), int(255 * (1 - x) + 233 * x))
            k.drain(10, per_read_ms=5)
            fixed(k, c, 0x07)
            time.sleep(1 / fps)
        a = input("\n  >> Did it FADE smoothly (s), STEP/jump (j), or stay FROZEN (f)? ").strip().lower()
        log(f"G{fps}: old-method fade at {fps}/s -> {a}")

    k.close()
    log("done")
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\nSaved to {OUT}. Tell Claude the ring test is done.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"ERROR: {e}")
        with open(OUT, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print("\nSomething failed - tell Claude (details saved).")
    input("\nPress Enter to close.")
