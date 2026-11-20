"""Kraken pump-ring test #4: is the GIF on the screen what slows the ring?

Every earlier test ran while the cooler was decoding a big GIF on its LCD. This
switches the LCD to the cooler's built-in liquid-temperature screen (almost no
work) and streams the ring again. Automatic; answer each step with one key.
Results: diagnostics\\ring_test3.txt. Your GIF comes back when RGB Studio starts.
"""
import colorsys
import os
import sys
import time

try:
    import msvcrt
except ImportError:
    msvcrt = None

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "kraken"))
import nzxt_kraken as nk  # noqa: E402

OUT = os.path.join(ROOT, "diagnostics", "ring_test3.txt")
os.makedirs(os.path.dirname(OUT), exist_ok=True)
lines = []


def log(s):
    print(s)
    lines.append(s)


def save():
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def hsv(h):
    r, g, b = colorsys.hsv_to_rgb((h % 360) / 360, 1, 1)
    return (int(r * 255), int(g * 255), int(b * 255))


def spin(k, fps, secs=5):
    t0 = time.time()
    nxt = t0
    busy = 0
    while time.time() - t0 < secs:
        k.write_leds(0x01, [hsv((time.time() - t0) * 90 + i * 15) for i in range(24)])
        nxt += 1 / fps
        while time.time() < nxt:
            p = k.read_pkt(5)
            if p is not None and p[0] == 0xFF and p[1] == 0x01:
                busy += 1
    return busy


def key(prompt):
    print(prompt, end="", flush=True)
    while True:
        c = msvcrt.getwch().lower() if msvcrt else (input() or " ")[0].lower()
        if c in "sjf":
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


def main():
    log("Kraken ring test 4  " + time.strftime("%Y-%m-%d %H:%M:%S"))
    k = nk.Kraken()
    k.open()
    log("connection: " + ("HID (shared)" if k.hid else "WinUSB"))
    k.drain(200)
    ok = k.switch_bucket(0, mode=0x02)   # LCD -> built-in liquid temperature screen
    log(f"LCD switched to liquid-temp screen: {ok}")
    print("\nThe screen should now show the cooler's own temperature display (no GIF).")
    time.sleep(2)
    k.lighting_handshake()

    steps = [("no GIF: 10 updates/s", 10), ("no GIF: 20 updates/s", 20), ("no GIF: 5 updates/s", 5)]
    for i, (name, fps) in enumerate(steps, 1):
        countdown(f"STEP {i} of {len(steps)}: {name}")
        busy = spin(k, fps)
        a = key("  How did it move?  s = smooth   j = jumpy   f = frozen/off : ")
        log(f"{name}: {a}   busy replies {busy}")
        save()
        k.write_leds(0x01, [(0, 0, 0)] * 24)
        time.sleep(1.5)

    k.close()
    log("done")
    save()
    print(f"\nAll done - saved to {OUT}.")
    print("Start RGB Studio again to bring your GIF back, and tell Claude the ring test is done.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"ERROR: {e}")
        save()
        print("\nSomething failed - tell Claude (details saved).")
    print("\nThis window closes in 10 seconds.")
    time.sleep(10)
