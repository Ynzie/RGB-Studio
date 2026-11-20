"""Kraken pump-ring test #3 (automatic): is the "save" flag what limits it?

Earlier tests: single updates work, streaming freezes above ~2/s. The 0xFF
0x01 replies turned out to arrive ~8/s no matter what we send, so they're
background reports, not refusals. New lead: the apply packet's last byte (0x01)
probably means "save to the cooler's memory" - a slow flash write that would
block it ~0.5 s per update. This tries streaming with that byte off, plus a
few related variants. Runs by itself; answer each step with one key.
Results: diagnostics\\ring_test2.txt
"""
import colorsys
import os
import sys
import time

try:
    import msvcrt
except ImportError:  # not Windows
    msvcrt = None

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "kraken"))
import nzxt_kraken as nk  # noqa: E402

OUT = os.path.join(ROOT, "diagnostics", "ring_test2.txt")
os.makedirs(os.path.dirname(OUT), exist_ok=True)
lines = []


def log(s):
    print(s)
    lines.append(s)


def save():
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def drain_for(k, ms):
    end = time.time() + ms / 1000
    while time.time() < end:
        k.read_pkt(5)


def buffer(k, colors):
    payload = bytearray()
    for (r, g, b) in colors:
        payload += bytes((g, r, b))
    k.write_cmd(bytes([0x22, 0x10, 0x01, 0x00]) + bytes(payload[:60]))
    k.write_cmd(bytes([0x22, 0x11, 0x01, 0x00]) + bytes(payload[60:120]))


def apply(k, b4=0x01, b7=0x28, b12=0x32, b15=0x01):
    k.write_cmd(bytes([0x22, 0xA0, 0x01, 0x00, b4, 0x00, 0x00, b7,
                       0x00, 0x00, 0x80, 0x00, b12, 0x00, 0x00, b15]))


def hsv(h):
    r, g, b = colorsys.hsv_to_rgb((h % 360) / 360, 1, 1)
    return (int(r * 255), int(g * 255), int(b * 255))


def spin(k, fps, secs=5, apply_every=True, **ap):
    t0 = time.time()
    nxt = t0
    if not apply_every:            # one apply up front, then colors only
        buffer(k, [hsv(i * 15) for i in range(24)])
        apply(k, **ap)
    while time.time() - t0 < secs:
        buffer(k, [hsv((time.time() - t0) * 90 + i * 15) for i in range(24)])
        if apply_every:
            apply(k, **ap)
        nxt += 1 / fps
        drain_for(k, max(0, (nxt - time.time()) * 1000))


def key(prompt, allowed):
    print(prompt, end="", flush=True)
    while True:
        if msvcrt:
            c = msvcrt.getwch().lower()
        else:
            c = (input() or " ")[0].lower()
        if c in allowed:
            print(c)
            return c


def countdown(title):
    print("\n" + "=" * 60)
    print("  " + title)
    print("  Watch the PUMP RING (the light around the screen).")
    for n in (3, 2, 1):
        print(f"  starting in {n}...", flush=True)
        time.sleep(1)
    print("  >>> WATCH NOW (5 seconds) <<<", flush=True)


def rest(k):
    buffer(k, [(0, 0, 0)] * 24)
    apply(k)
    drain_for(k, 1500)


def main():
    log("Kraken ring test 3  " + time.strftime("%Y-%m-%d %H:%M:%S"))
    k = nk.Kraken()
    k.open()
    log("connection: " + ("HID (shared)" if k.hid else "WinUSB"))
    k.lighting_handshake()

    # Background replies with nothing being sent (are 0xFF 0x01 just chatter?)
    t0 = time.time()
    counts = {}
    while time.time() - t0 < 2:
        p = k.read_pkt(20)
        if p is not None:
            kk = bytes(p[:2]).hex()
            counts[kk] = counts.get(kk, 0) + 1
    log(f"idle replies in 2 s: {counts}")

    steps = [
        ("save flag OFF, 10 updates/s", dict(fps=10, b15=0x00)),
        ("save flag OFF, 20 updates/s", dict(fps=20, b15=0x00)),
        ("save flag OFF + byte7=0, 10/s", dict(fps=10, b15=0x00, b7=0x00)),
        ("save flag OFF + byte12=0, 10/s", dict(fps=10, b15=0x00, b12=0x00)),
        ("apply once, then colors only, 10/s", dict(fps=10, apply_every=False, b15=0x00)),
        ("save flag = 2, 10/s", dict(fps=10, b15=0x02)),
        ("normal packet, 1.5 updates/s (for comparison)", dict(fps=1.5)),
    ]
    for i, (name, cfg) in enumerate(steps, 1):
        countdown(f"STEP {i} of {len(steps)}: {name}")
        cfg = dict(cfg)
        fps = cfg.pop("fps")
        every = cfg.pop("apply_every", True)
        spin(k, fps, apply_every=every, **cfg)
        a = key("  How did it move?  s = smooth   j = jumpy   f = frozen/off : ", "sjf")
        log(f"{name}: {a}")
        save()
        rest(k)

    k.close()
    log("done")
    save()
    print(f"\nAll done - saved to {OUT}. Tell Claude the ring test is done.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"ERROR: {e}")
        save()
        print("\nSomething failed - tell Claude (details saved).")
    print("\nThis window closes in 10 seconds.")
    time.sleep(10)
