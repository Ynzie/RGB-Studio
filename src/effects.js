// Effect engine: each effect maps (time, ledCount, params) -> array of {red,green,blue}
// params: { speed: 0-100, brightness: 0-100, colors: [ '#rrggbb', ... ], scale: 0-100 }
//
// Every effect runs off the SAME clock (t, seconds) on every device, so all
// devices stay in step. Patterns are sized to each strip/fan/ring: the Scale
// slider sets how many WHOLE times the pattern repeats along it, so every
// device shows the complete effect (a 24-LED ring shows the full palette, not
// a 20% slice of a 110-LED wave) and rings wrap around without a seam.
//
// Loaded by the main process (require) AND by the UI for the effect previews
// (<script src="../effects.js"> -> window.RGBEffects).
(function () {
  function hexToRgb(hex) {
    const n = parseInt(String(hex || '#000000').replace('#', ''), 16) || 0;
    return { red: (n >> 16) & 255, green: (n >> 8) & 255, blue: n & 255 };
  }

  function hsvToRgb(h, s, v) {
    h = ((h % 360) + 360) % 360;
    const c = v * s, x = c * (1 - Math.abs(((h / 60) % 2) - 1)), m = v - c;
    let r = 0, g = 0, b = 0;
    if (h < 60) [r, g, b] = [c, x, 0];
    else if (h < 120) [r, g, b] = [x, c, 0];
    else if (h < 180) [r, g, b] = [0, c, x];
    else if (h < 240) [r, g, b] = [0, x, c];
    else if (h < 300) [r, g, b] = [x, 0, c];
    else [r, g, b] = [c, 0, x];
    return {
      red: Math.round((r + m) * 255),
      green: Math.round((g + m) * 255),
      blue: Math.round((b + m) * 255),
    };
  }

  function scaleColor(c, f) {
    f = Math.max(0, Math.min(1, f));
    return {
      red: Math.round(c.red * f),
      green: Math.round(c.green * f),
      blue: Math.round(c.blue * f),
    };
  }

  function lerpColor(a, b, t) {
    return {
      red: Math.round(a.red + (b.red - a.red) * t),
      green: Math.round(a.green + (b.green - a.green) * t),
      blue: Math.round(a.blue + (b.blue - a.blue) * t),
    };
  }

  function rgbToHsv(c) {
    const r = c.red / 255, g = c.green / 255, b = c.blue / 255;
    const max = Math.max(r, g, b), min = Math.min(r, g, b), d = max - min;
    let h = 0;
    if (d) {
      if (max === r) h = 60 * (((g - b) / d) % 6);
      else if (max === g) h = 60 * ((b - r) / d + 2);
      else h = 60 * ((r - g) / d + 4);
    }
    return { h: (h + 360) % 360, s: max ? d / max : 0, v: max };
  }

  // Blend through hue space (shortest arc) so transitions stay SATURATED.
  // Raw RGB lerp between e.g. pink and cyan passes through a washed-out pale
  // middle that reads as white on LEDs.
  function lerpColorHsv(a, b, t) {
    const A = rgbToHsv(a), B = rgbToHsv(b);
    // Colors with no saturation (white/black/greys) have no meaningful hue —
    // fall back to RGB lerp so palettes that DO contain white still work.
    if (A.s < 0.05 || B.s < 0.05) return lerpColor(a, b, t);
    let dh = B.h - A.h;
    if (dh > 180) dh -= 360;
    if (dh < -180) dh += 360;
    return hsvToRgb(A.h + dh * t, A.s + (B.s - A.s) * t, A.v + (B.v - A.v) * t);
  }

  // Position 0..1 around the palette -> smoothly blended color.
  function paletteAt(cols, pos) {
    pos = ((pos % 1) + 1) % 1;
    const seg = pos * cols.length;
    const idx = Math.floor(seg) % cols.length;
    const next = (idx + 1) % cols.length;
    return lerpColorHsv(cols[idx], cols[next], seg - Math.floor(seg));
  }

  const BLACK = { red: 0, green: 0, blue: 0 };
  const fill = (count, c) => Array.from({ length: count }, () => ({ ...c }));
  const palette = (p, fallback) => (p.colors.length ? p.colors : fallback).map(hexToRgb);
  // Scale slider -> how many WHOLE times a pattern repeats along a device.
  // Density feels the same as a fixed wavelength (Scale 100 ~ 10 LEDs per
  // cycle, 50 ~ 110), but rounded to whole cycles - never less than one - so
  // every device shows the complete pattern and rings wrap without a seam.
  // 0 (scale <= 2) = no spatial pattern: the whole device is one color.
  function repeats(scale, count, density = 1) {
    if (scale <= 2) return 0;
    const lambda = Math.max(10, 210 - scale * 2) / density; // LEDs per cycle
    return Math.max(1, Math.round(count / lambda));
  }

  // Per-device state for the effects that need memory (sparkle, fire, ...).
  function stateFor(map, key, count, init) {
    let s = map.get(key);
    if (!s || s.count !== count) {
      s = { count, lastT: null, ...init(count) };
      map.set(key, s);
    }
    return s;
  }
  function stepTime(s, t) {
    const dt = s.lastT == null ? 0 : Math.min(0.25, Math.max(0, t - s.lastT));
    s.lastT = t;
    return dt;
  }

  const EFFECTS = {
    static: {
      name: 'Static',
      desc: 'One solid color',
      still: true,
      fn(t, count, p) {
        return fill(count, hexToRgb(p.colors[0] || '#ff0000'));
      },
    },

    rainbowWave: {
      name: 'Rainbow Wave',
      desc: 'Rainbow rolling across every device (always rainbow - ignores your colors)',
      noColors: true,
      fn(t, count, p) {
        const speed = (p.speed / 100) * 360; // deg per second
        const n = repeats(p.scale, count);
        return Array.from({ length: count }, (_, i) => hsvToRgb(t * speed + (i / count) * 360 * n, 1, 1));
      },
    },

    spectrumCycle: {
      name: 'Rainbow Cycle',
      desc: 'Everything fades through the whole rainbow together (ignores your colors - use Color Cycle for yours)',
      noColors: true,
      fn(t, count, p) {
        return fill(count, hsvToRgb(t * (p.speed / 100) * 180, 1, 1));
      },
    },

    colorCycle: {
      name: 'Color Cycle',
      desc: 'Everything fades through your colors together',
      fn(t, count, p) {
        const cols = palette(p, ['#ff0000', '#0000ff']);
        const rate = 0.05 + (p.speed / 100) * 0.6; // palette steps per second
        return fill(count, paletteAt(cols, (t * rate) / cols.length));
      },
    },

    breathing: {
      name: 'Breathing',
      desc: 'Slow fade in and out (cycles your colors)',
      fn(t, count, p) {
        const cols = palette(p, ['#ff0000']);
        const speed = 0.2 + (p.speed / 100) * 1.5; // breaths per second
        const cycle = t * speed;
        const phase = (1 - Math.cos((cycle % 1) * Math.PI * 2)) / 2; // starts dark
        const base = cols[Math.floor(cycle) % cols.length];
        return fill(count, scaleColor(base, phase));
      },
    },

    heartbeat: {
      name: 'Heartbeat',
      desc: 'Double pulse, like a heartbeat',
      fn(t, count, p) {
        const base = hexToRgb(p.colors[0] || '#ff0033');
        const period = 1.6 - (p.speed / 100) * 1.0; // seconds per beat
        const x = (t % period) / period;
        const pulse = (c, w) => Math.exp(-Math.pow((x - c) / w, 2));
        return fill(count, scaleColor(base, Math.max(pulse(0.1, 0.05), 0.65 * pulse(0.3, 0.06))));
      },
    },

    colorShiftWave: {
      name: 'Color Shift Wave',
      desc: 'Your colors flowing across every device',
      fn(t, count, p) {
        const cols = palette(p, ['#ff0000', '#0000ff']);
        const speed = (p.speed / 100) * 2;
        const n = repeats(p.scale, count); // 0 = uniform color, perfectly synced everywhere
        return Array.from({ length: count }, (_, i) => paletteAt(cols, t * speed + (n * i) / count));
      },
    },

    gradient: {
      name: 'Gradient',
      desc: 'Your colors spread out, standing still',
      still: true,
      fn(t, count, p, key, opts) {
        const cols = palette(p, ['#ff0000', '#0000ff']);
        if (count <= 1 || cols.length < 2) return fill(count, cols[0]);
        // Spread the palette once along each device. On a ring it goes there
        // and back (first color at the joint) so there's no hard seam.
        return Array.from({ length: count }, (_, i) => {
          let f = i / (count - 1);
          if (opts.ring) { f = (2 * i) / count; if (f > 1) f = 2 - f; }
          const pos = f * (cols.length - 1);
          const idx = Math.min(cols.length - 1, Math.floor(pos));
          const next = Math.min(cols.length - 1, idx + 1);
          return lerpColorHsv(cols[idx], cols[next], pos - idx);
        });
      },
    },

    aurora: {
      name: 'Aurora',
      desc: 'Soft drifting bands of your colors',
      fn(t, count, p) {
        const cols = palette(p, ['#00ffaa', '#7c5cff', '#00aaff']);
        const s = 0.5 + (p.speed / 100) * 2.5; // drift (rad/s): visible even at low speed
        const n = Math.max(1, repeats(p.scale, count));
        return Array.from({ length: count }, (_, i) => {
          const x = (2 * Math.PI * n * i) / count; // whole periods: seamless on rings
          const pos = 0.5 + 0.25 * Math.sin(x + t * s) + 0.25 * Math.sin(2 * x - t * s * 0.6);
          const glow = 0.45 + 0.55 * (0.5 + 0.5 * Math.sin(x - t * s * 1.3));
          return scaleColor(paletteAt(cols, pos), glow);
        });
      },
    },

    wave: {
      name: 'Pulse Wave',
      desc: 'Waves of light rolling along',
      fn(t, count, p) {
        const base = hexToRgb(p.colors[0] || '#00aaff');
        const n = Math.max(1, repeats(p.scale, count, 3)); // pulses along the device
        const rate = 0.25 + (p.speed / 100) * 1.75;    // pulses passing per second
        return Array.from({ length: count }, (_, i) => {
          const ph = ((n * i) / count - t * rate) * Math.PI * 2;
          return scaleColor(base, Math.pow(0.5 + 0.5 * Math.sin(ph), 2.2));
        });
      },
    },

    comet: {
      name: 'Comet',
      desc: 'A bright head with a fading tail',
      fn(t, count, p) {
        const laps = 0.15 + (p.speed / 100) * 1.1;    // laps per second, same on every device
        const tail = Math.max(2, Math.round(count * (0.15 + (p.scale / 100) * 0.5)));
        const head = ((t * laps) % 1) * count;
        const base = hexToRgb(p.colors[0] || '#00aaff');
        return Array.from({ length: count }, (_, i) => {
          let d = head - i;
          if (d < 0) d += count;
          return d < tail ? scaleColor(base, Math.pow(1 - d / tail, 2)) : { ...BLACK };
        });
      },
    },

    scanner: {
      name: 'Scanner',
      desc: 'A bar sweeping back and forth',
      fn(t, count, p) {
        const base = hexToRgb(p.colors[0] || '#ff0000');
        const period = 3.2 - (p.speed / 100) * 2.6; // seconds per sweep and back
        const x = (t % period) / period;
        const tri = x < 0.5 ? x * 2 : 2 - x * 2;  // 0 -> 1 -> 0
        const ease = tri * tri * (3 - 2 * tri);    // slow down at the ends
        const pos = ease * Math.max(0, count - 1);
        const width = Math.max(1, 1 + (p.scale / 100) * 4);
        return Array.from({ length: count }, (_, i) =>
          scaleColor(base, Math.max(0, 1 - Math.abs(i - pos) / width)));
      },
    },

    chase: {
      name: 'Chase',
      desc: 'Marching dashes in your colors',
      fn(t, count, p) {
        const cols = palette(p, ['#ff0000', '#ffffff']);
        // Whole dash pairs along the device (seamless on rings), ~3-4 LEDs per dash.
        const want = 2 + (p.scale / 100) * 4;
        let pairs = Math.max(1, Math.round(count / (2 * want)));
        // Multiple of the color count so the color order also wraps cleanly.
        if (pairs >= cols.length) pairs = Math.round(pairs / cols.length) * cols.length;
        const rate = 0.5 + (p.speed / 100) * 5;       // dashes moving per second
        return Array.from({ length: count }, (_, i) => {
          const k = Math.floor((i / count) * 2 * pairs - t * rate);
          if (((k % 2) + 2) % 2) return { ...BLACK };
          const ci = (((k / 2) % cols.length) + cols.length) % cols.length;
          return { ...cols[Math.floor(ci)] };
        });
      },
    },

    sparkle: {
      name: 'Sparkle',
      desc: 'Random white (or first color) flashes',
      _state: new Map(),
      fn(t, count, p, key) {
        const s = stateFor(this._state, key ?? count, count, (n) => ({ vals: new Array(n).fill(0) }));
        const frames = stepTime(s, t) * 30; // tuned originally at 30 fps
        const decay = (0.02 + (p.speed / 100) * 0.15) * frames;
        const spawn = (0.002 + (p.speed / 100) * 0.03) * frames;
        const base = hexToRgb(p.colors[0] || '#ffffff');
        for (let i = 0; i < count; i++) {
          s.vals[i] = Math.max(0, s.vals[i] - decay);
          if (Math.random() < spawn) s.vals[i] = 1;
        }
        return s.vals.map((v) => scaleColor(base, v));
      },
    },

    twinkle: {
      name: 'Twinkle',
      desc: 'Stars softly fading in and out in your colors',
      _state: new Map(),
      fn(t, count, p, key) {
        const s = stateFor(this._state, key ?? count, count, (n) => ({
          life: new Array(n).fill(0), col: new Array(n).fill(0),
        }));
        const dt = stepTime(s, t);
        const cols = palette(p, ['#ffffff', '#7c5cff', '#00d4ff']);
        const dur = 2.4 - (p.speed / 100) * 1.8;         // seconds per twinkle
        const density = 0.08 + (p.scale / 100) * 0.35;   // share of LEDs lit
        return Array.from({ length: count }, (_, i) => {
          if (s.life[i] > 0) s.life[i] = Math.max(0, s.life[i] - dt / dur);
          else if (Math.random() < (density / dur) * dt) {
            s.life[i] = 1;
            s.col[i] = Math.floor(Math.random() * cols.length);
          }
          const l = s.life[i];
          return l > 0 ? scaleColor(cols[s.col[i] % cols.length], Math.sin(l * Math.PI)) : { ...BLACK };
        });
      },
    },

    fire: {
      name: 'Fire',
      desc: 'Flickering flames (always fire colors)',
      noColors: true,
      _state: new Map(),
      fn(t, count, p, key, opts) {
        // On a ring, flames rise up BOTH sides from the joint and meet
        // opposite it, so the fire wraps around with no jump.
        const total = count;
        if (opts.ring && count >= 6) count = Math.ceil(count / 2);
        const s = stateFor(this._state, key ?? count, count, (n) => ({
          heat: new Array(n).fill(0), prev: new Array(n).fill(0), acc: 0,
        }));
        s.acc += stepTime(s, t);
        // Speed = how fast the flames flicker (simulation steps per second);
        // frames in between are blended, so it flows instead of jittering.
        const rate = 6 + (p.speed / 100) * 24;
        // Cooling scaled by length so flames reach ~the top on any device.
        const maxCool = (0.9 + (1 - p.scale / 100) * 1.6) / Math.max(4, count);
        const sparking = 0.55;
        const sparkZone = Math.max(1, Math.round(count * 0.12));
        while (s.acc >= 1 / rate) {
          s.acc -= 1 / rate;
          s.prev = s.heat.slice();
          const h = s.heat;
          for (let i = 0; i < count; i++) h[i] = Math.max(0, h[i] - Math.random() * maxCool);
          for (let i = count - 1; i >= 2; i--) h[i] = (h[i - 1] + h[i - 2] * 2) / 3;
          if (count > 1) h[1] = (h[0] + h[1]) / 2;
          if (Math.random() < sparking) {
            const y = Math.floor(Math.random() * sparkZone);
            h[y] = Math.min(1, h[y] + 0.6 + Math.random() * 0.4);
          }
        }
        const f = Math.min(1, s.acc * rate);
        const blended = s.heat.map((v, i) => s.prev[i] + (v - s.prev[i]) * f);
        const heatAt = total === count ? (i) => blended[i] : (i) => blended[Math.min(i, total - 1 - i)];
        return Array.from({ length: total }, (_, i) => heatAt(i)).map((v) => {
          // black -> red -> orange -> yellow
          if (v < 0.4) return { red: Math.round((v / 0.4) * 255), green: 0, blue: 0 };
          if (v < 0.8) return { red: 255, green: Math.round(((v - 0.4) / 0.4) * 140), blue: 0 };
          return { red: 255, green: 140 + Math.round(((v - 0.8) / 0.2) * 100), blue: Math.round(((v - 0.8) / 0.2) * 40) };
        });
      },
    },

    candle: {
      name: 'Candle',
      desc: 'Gentle flicker in your first color',
      _state: new Map(),
      fn(t, count, p, key) {
        const s = stateFor(this._state, key ?? count, count, () => ({ level: 1, target: 1, hold: 0 }));
        const dt = stepTime(s, t);
        s.hold -= dt;
        if (s.hold <= 0) {
          s.target = 0.55 + Math.random() * 0.45;
          s.hold = 0.05 + Math.random() * (0.35 - (p.speed / 100) * 0.25);
        }
        s.level += (s.target - s.level) * Math.min(1, dt * 12);
        return fill(count, scaleColor(hexToRgb(p.colors[0] || '#ff7a1a'), s.level));
      },
    },
  };

  EFFECTS.off = {
    name: 'Off',
    desc: 'Lights off',
    noColors: true,
    still: true,
    fn(t, count) { return fill(count, BLACK); },
  };

  // key: stable per-device id, used only by stateful effects (Sparkle etc.).
  // opts.ring: the LEDs form a closed circle (fan, pump ring) - effects with a
  // start and an end (Fire, Gradient) mirror so they wrap without a jump.
  function renderEffect(effectId, t, ledCount, params, key, opts) {
    const eff = EFFECTS[effectId] || EFFECTS.static;
    params = params || {};
    const colors0 = Array.isArray(params.colors) && params.colors.length ? params.colors : ['#ff0000'];
    const p = {
      speed: params.speed ?? 50,
      brightness: params.brightness ?? 100,
      scale: params.scale ?? 50,
      colors: colors0,
    };
    const count = Math.max(0, ledCount | 0);
    if (!count) return [];
    const colors = eff.fn(t, count, p, key, opts || {});
    const b = p.brightness / 100;
    return b >= 0.999 ? colors : colors.map((c) => scaleColor(c, b));
  }

  // ---------- RGBW strips on RGB (3-byte) headers ----------
  // Some strips (e.g. the light bar on some GPUs synced to a motherboard ARGB
  // header) are RGBW: every LED consumes FOUR bytes (incl. a white chip), but the
  // header sends THREE per LED slot. Sent as normal RGB the bytes drift across
  // LED boundaries -> a whitish, jumbled mess. Fix: render the effect for the
  // PHYSICAL LED count, convert to RGBW, and re-pack the 4-byte stream into
  // 3-byte header slots (needs ceil(4*N/3) slots: 29 LEDs -> 39 slots).
  //   mode 'rgbw'     header sends G,R,B (WS2812 order), LEDs read G,R,B,W (SK6812 RGBW)
  //   mode 'rgbw-rg'  same, but LEDs read R,G,B,W   (fixes a red/green swap)
  //   mode 'rgbw-alt' header sends R,G,B instead     (fixes a still-jumbled strip)
  function rgbwSlots(physical) {
    return Math.ceil((4 * physical) / 3);
  }
  function packRgbw(phys, slots, mode) {
    const stream = [];
    for (const c of phys) {
      let r = c.red, g = c.green, b = c.blue;
      const w = Math.min(r, g, b); // move the shared part onto the white chip
      r -= w; g -= w; b -= w;
      if (mode === 'rgbw-rg') stream.push(r, g, b, w);
      else stream.push(g, r, b, w);
    }
    const out = [];
    for (let j = 0; j < slots; j++) {
      const x = stream[3 * j] ?? 0, y = stream[3 * j + 1] ?? 0, z = stream[3 * j + 2] ?? 0;
      out.push(mode === 'rgbw-alt' ? { red: x, green: y, blue: z } : { red: y, green: x, blue: z });
    }
    return out;
  }

  // Animated effects first, then the still ones (Gradient, Static).
  function listEffects() {
    const all = Object.entries(EFFECTS).map(([id, e]) => ({ id, name: e.name, desc: e.desc || '', still: !!e.still, noColors: !!e.noColors }));
    return all.filter((e) => !e.still).concat(all.filter((e) => e.still));
  }

  const api = { renderEffect, listEffects, packRgbw, rgbwSlots };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else window.RGBEffects = api;
})();
