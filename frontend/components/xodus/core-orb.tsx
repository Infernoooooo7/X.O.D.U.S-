"use client";

import { useEffect, useRef, useState } from "react";
import { onCorePulse, type Core, type CoreState } from "@/lib/core-state";
import { cn } from "@/lib/utils";

/*
  The core: a particle sphere with two orbits, drawn on a 2D canvas (no 3D engine, no image files).
  It shows only real state (see lib/core-state.ts): the look eases towards the state's targets, and real events
  (a token arriving, a task step starting) add a short flare. Task steps that have started orbit as satellites.

  Cost control: at most 30 frames per second (24 when calm), device pixel ratio capped at 1.5, nothing drawn while the
  tab is hidden or the core is scrolled out of view, and the loop stops entirely once an offline core has settled.
  With prefers-reduced-motion the loop never runs: one still frame is drawn per state change, in the state's colours.
*/

type Rgb = [number, number, number];
type PaletteName = "core" | "warn" | "bad" | "dim";
type Look = { speed: number; scale: number; jitter: number; bright: number; swirl: number; fps: number; palette: PaletteName };

const LOOK: Record<CoreState, Look> = {
  idle: { speed: 0.14, scale: 1, jitter: 0.012, bright: 0.72, swirl: 0, fps: 24, palette: "core" },
  thinking: { speed: 0.55, scale: 0.86, jitter: 0.05, bright: 0.92, swirl: 1.1, fps: 30, palette: "core" },
  streaming: { speed: 0.32, scale: 1, jitter: 0.02, bright: 1, swirl: 0.2, fps: 30, palette: "core" },
  working: { speed: 0.4, scale: 0.95, jitter: 0.03, bright: 0.9, swirl: 0.45, fps: 30, palette: "core" },
  waiting: { speed: 0.06, scale: 1, jitter: 0, bright: 0.88, swirl: 0, fps: 24, palette: "warn" },
  error: { speed: 0.08, scale: 0.96, jitter: 0.07, bright: 0.82, swirl: 0, fps: 24, palette: "bad" },
  offline: { speed: 0.02, scale: 0.92, jitter: 0, bright: 0.36, swirl: 0, fps: 0, palette: "dim" },
};
const SETTLE_MS = 1600; // how long the loop runs after a change, even for a core that then stands still

function hexToRgb(value: string, fallback: Rgb): Rgb {
  const m = /^#([0-9a-f]{6})$/i.exec(value.trim());
  if (!m) return fallback;
  const n = parseInt(m[1], 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function readPalettes(): { light: boolean; palettes: Record<PaletteName, { dot: Rgb; ring: Rgb; glow: Rgb }> } {
  const css = getComputedStyle(document.documentElement);
  const v = (name: string, fallback: Rgb) => hexToRgb(css.getPropertyValue(name), fallback);
  const teal: Rgb = [79, 216, 204];
  return {
    light: !document.documentElement.classList.contains("dark"),
    palettes: {
      core: { dot: v("--core-1", teal), ring: v("--core-2", teal), glow: v("--core-3", teal) },
      warn: { dot: v("--warn", [245, 192, 96]), ring: v("--warn", [245, 192, 96]), glow: v("--warn", [245, 192, 96]) },
      bad: { dot: v("--destructive", [255, 143, 128]), ring: v("--destructive", [255, 143, 128]), glow: v("--destructive", [255, 143, 128]) },
      dim: { dot: v("--muted-foreground", [143, 164, 174]), ring: v("--rail", [42, 58, 69]), glow: v("--rail", [42, 58, 69]) },
    },
  };
}

type Particle = { x: number; y: number; z: number; seed: number; size: number };

/** Evenly spread points on a unit sphere (Fibonacci lattice), with deterministic per-point variation. */
function sphere(n: number): Particle[] {
  const golden = Math.PI * (3 - Math.sqrt(5));
  return Array.from({ length: n }, (_, i) => {
    const y = 1 - (i / (n - 1)) * 2;
    const r = Math.sqrt(1 - y * y);
    return { x: Math.cos(golden * i) * r, y, z: Math.sin(golden * i) * r, seed: ((i * 9301 + 49297) % 233280) / 233280,
      size: 0.6 + ((i * 7919) % 100) / 110 };
  });
}

function makeSprite(): HTMLCanvasElement {
  const c = document.createElement("canvas");
  c.width = c.height = 32;
  const g = c.getContext("2d")!;
  const grad = g.createRadialGradient(16, 16, 0, 16, 16, 16);
  grad.addColorStop(0, "rgba(255,255,255,1)");
  grad.addColorStop(0.22, "rgba(255,255,255,0.9)");
  grad.addColorStop(0.55, "rgba(255,255,255,0.2)");
  grad.addColorStop(1, "rgba(255,255,255,0)");
  g.fillStyle = grad;
  g.fillRect(0, 0, 32, 32);
  return c;
}

function tint(base: HTMLCanvasElement, out: HTMLCanvasElement, rgb: Rgb) {
  const g = out.getContext("2d")!;
  g.globalCompositeOperation = "source-over";
  g.clearRect(0, 0, 32, 32);
  g.drawImage(base, 0, 0);
  g.globalCompositeOperation = "source-in";
  g.fillStyle = `rgb(${rgb[0]},${rgb[1]},${rgb[2]})`;
  g.fillRect(0, 0, 32, 32);
  g.globalCompositeOperation = "source-over";
}

const rgba = (c: Rgb, a: number) => `rgba(${c[0] | 0},${c[1] | 0},${c[2] | 0},${Math.max(0, Math.min(1, a))})`;

type Engine = { update: () => void; destroy: () => void };

function createEngine(canvas: HTMLCanvasElement, getState: () => CoreState, getSatellites: () => number, hero: boolean,
  setAnimating: (on: boolean) => void): Engine | null {
  const ctx = canvas.getContext("2d");
  if (!ctx) return null;
  const reduce = matchMedia("(prefers-reduced-motion: reduce)");
  let { light, palettes } = readPalettes();
  const base = makeSprite();
  const dotSprite = document.createElement("canvas");
  const ringSprite = document.createElement("canvas");
  dotSprite.width = dotSprite.height = ringSprite.width = ringSprite.height = 32;

  let w = 0, h = 0, dpr = 1;
  let points = sphere(hero ? 420 : 110);
  const start = getState();
  const cur = { ...LOOK[start], dot: [...palettes[LOOK[start].palette].dot] as Rgb, ring: [...palettes[LOOK[start].palette].ring] as Rgb,
    glow: [...palettes[LOOK[start].palette].glow] as Rgb };
  let tinted: [Rgb, Rgb] | null = null;
  let angle = 0, energy = 0, lastDraw = 0, raf = 0, running = false;
  let visible = document.visibilityState === "visible", inView = true;
  let changedAt = performance.now(), settleUntil = changedAt + SETTLE_MS;

  const wanted = () => !reduce.matches && visible && inView && w > 0 && (LOOK[getState()].fps > 0 || performance.now() < settleUntil);

  function retint() {
    const same = (a: Rgb, b: Rgb) => Math.abs(a[0] - b[0]) + Math.abs(a[1] - b[1]) + Math.abs(a[2] - b[2]) < 3;
    if (tinted && same(tinted[0], cur.dot) && same(tinted[1], cur.ring)) return;
    tint(base, dotSprite, cur.dot);
    tint(base, ringSprite, cur.ring);
    tinted = [[...cur.dot] as Rgb, [...cur.ring] as Rgb];
  }

  function ease(dt: number, snap: boolean) {
    const look = LOOK[getState()];
    const pal = palettes[look.palette];
    const k = snap ? 1 : 1 - Math.exp(-dt * 3);
    for (const key of ["speed", "scale", "jitter", "bright", "swirl"] as const) cur[key] += (look[key] - cur[key]) * k;
    for (const key of ["dot", "ring", "glow"] as const) for (let i = 0; i < 3; i++) cur[key][i] += (pal[key][i] - cur[key][i]) * k;
    energy *= snap ? 0 : Math.exp(-dt * 2.4);
  }

  function ring(cx: number, cy: number, radius: number, tiltX: number, tiltZ: number, rot: number, n: number, alpha: number, size: number) {
    const cX = Math.cos(tiltX), sX = Math.sin(tiltX), cZ = Math.cos(tiltZ), sZ = Math.sin(tiltZ);
    for (let i = 0; i < n; i++) {
      const a = (i / n) * Math.PI * 2 + rot;
      const x = Math.cos(a), z = Math.sin(a);
      const y1 = -z * sX, z1 = z * cX;
      const x2 = x * cZ - y1 * sZ, y2 = x * sZ + y1 * cZ;
      const depth = (z1 + 1) / 2;
      ctx!.globalAlpha = Math.min(1, (0.05 + 0.55 * depth) * alpha);
      const s = size * (0.6 + 0.8 * depth);
      ctx!.drawImage(ringSprite, cx + x2 * radius - s / 2, cy + y2 * radius - s / 2, s, s);
    }
  }

  function draw(now: number) {
    const state = getState();
    const t = now / 1000;
    ctx!.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx!.clearRect(0, 0, w, h);
    retint();
    const breathe = state === "idle" || state === "waiting" ? Math.sin(t * 1.3) * 0.014 : Math.sin(t * 2.6) * 0.006;
    const flicker = state === "error" && now - changedAt < 1500 ? 0.72 + 0.28 * Math.sin(t * 23) : 1;
    const bright = cur.bright * flicker * (light ? 0.8 : 1);
    const cx = w / 2, cy = h * (hero ? 0.46 : 0.5);
    const R = Math.min(w, h) * (hero ? 0.27 : 0.3) * (cur.scale + breathe + energy * 0.05);
    ctx!.globalCompositeOperation = light ? "source-over" : "lighter";

    const glow = ctx!.createRadialGradient(cx, cy, R * 0.05, cx, cy, R * 1.65);
    glow.addColorStop(0, rgba(cur.glow, (light ? 0.22 : 0.4) * bright * (1 + energy * 0.5)));
    glow.addColorStop(0.45, rgba(cur.ring, (light ? 0.08 : 0.12) * bright));
    glow.addColorStop(1, rgba(cur.ring, 0));
    ctx!.fillStyle = glow;
    ctx!.beginPath();
    ctx!.arc(cx, cy, R * 1.65, 0, Math.PI * 2);
    ctx!.fill();
    const nucleus = ctx!.createRadialGradient(cx, cy, 0, cx, cy, R * 0.6); // the bright heart of the sphere
    nucleus.addColorStop(0, rgba(cur.dot, (light ? 0.16 : 0.3) * bright * (1 + energy)));
    nucleus.addColorStop(1, rgba(cur.dot, 0));
    ctx!.fillStyle = nucleus;
    ctx!.beginPath();
    ctx!.arc(cx, cy, R * 0.6, 0, Math.PI * 2);
    ctx!.fill();

    if (hero) { // the light the core casts on the floor below it
      ctx!.save();
      ctx!.translate(cx, cy + R * 1.55);
      ctx!.scale(1, 0.14);
      const floor = ctx!.createRadialGradient(0, 0, 0, 0, 0, R * 1.7);
      floor.addColorStop(0, rgba(cur.ring, (light ? 0.22 : 0.34) * bright));
      floor.addColorStop(1, rgba(cur.ring, 0));
      ctx!.fillStyle = floor;
      ctx!.beginPath();
      ctx!.arc(0, 0, R * 1.7, 0, Math.PI * 2);
      ctx!.fill();
      ctx!.restore();
    }

    const pulse = state === "waiting" ? 0.65 + 0.35 * Math.sin(t * 2.2) : 1;
    ring(cx, cy, R * 1.32, 1.2, -0.36, angle * 0.6, hero ? 150 : 46, bright * pulse, hero ? 2.8 : 1.8);
    ring(cx, cy, R * 1.56, 1.3, 0.5, -angle * 0.42, hero ? 170 : 52, bright * 0.75 * pulse, hero ? 2.4 : 1.6);

    const ca = Math.cos(angle), sa = Math.sin(angle), ct = Math.cos(0.38), st = Math.sin(0.38);
    for (const p of points) {
      const twist = cur.swirl * p.y * Math.sin(t * 0.9 + p.seed * 2);
      const c2 = Math.cos(twist), s2 = Math.sin(twist);
      const x0 = p.x * c2 + p.z * s2, z0 = -p.x * s2 + p.z * c2;
      const x1 = x0 * ca + z0 * sa, z1 = -x0 * sa + z0 * ca;
      const y1 = p.y * ct - z1 * st, z2 = p.y * st + z1 * ct;
      const j = 1 + cur.jitter * Math.sin(t * (2 + p.seed * 3) + p.seed * 40) + energy * 0.05 * Math.sin(p.seed * 90 + t * 6);
      const persp = 1 / (1.15 - 0.15 * z2);
      const depth = (z2 + 1) / 2;
      ctx!.globalAlpha = Math.min(1, (0.07 + 0.93 * depth * depth) * bright);
      const s = (hero ? 5.2 : 2.8) * p.size * persp * (0.7 + 0.6 * depth) * (1 + energy * 0.25);
      ctx!.drawImage(dotSprite, cx + x1 * R * j * persp - s / 2, cy + y1 * R * j * persp - s / 2, s, s);
    }

    const satellites = Math.min(6, getSatellites());
    if (satellites && (state === "working" || state === "waiting")) {
      const cX = Math.cos(1.2), sX = Math.sin(1.2), cZ = Math.cos(-0.36), sZ = Math.sin(-0.36);
      for (let k = 0; k < satellites; k++) {
        const a = angle * 2.2 + (k / satellites) * Math.PI * 2;
        const x = Math.cos(a), z = Math.sin(a);
        const y1 = -z * sX, z1 = z * cX;
        const sx = cx + (x * cZ - y1 * sZ) * R * 1.32, sy = cy + (x * sZ + y1 * cZ) * R * 1.32;
        const s = (hero ? 11 : 6) * (0.7 + 0.5 * ((z1 + 1) / 2));
        ctx!.globalAlpha = 0.95;
        ctx!.drawImage(dotSprite, sx - s / 2, sy - s / 2, s, s);
      }
    }
    ctx!.globalAlpha = 1;
    ctx!.globalCompositeOperation = "source-over";
  }

  function frame(now: number) {
    if (!wanted()) {
      running = false;
      raf = 0;
      setAnimating(false);
      if (reduce.matches) still();
      return;
    }
    raf = requestAnimationFrame(frame);
    const fps = LOOK[getState()].fps || 30;
    if (lastDraw && now - lastDraw < 1000 / fps - 2) return;
    const dt = lastDraw ? Math.min(0.1, (now - lastDraw) / 1000) : 1 / fps;
    lastDraw = now;
    ease(dt, false);
    angle += dt * (cur.speed + energy * 0.25);
    draw(now);
  }

  /** One frame with every value at its target: used for reduced motion and whenever nothing is animating. */
  function still() {
    if (!w) return;
    ease(0, true);
    draw(performance.now());
  }

  function ensure() {
    if (running) return;
    if (wanted()) {
      running = true;
      lastDraw = 0;
      setAnimating(true);
      raf = requestAnimationFrame(frame);
    } else {
      still();
    }
  }

  const resize = new ResizeObserver(([entry]) => {
    w = entry.contentRect.width;
    h = entry.contentRect.height;
    dpr = Math.min(window.devicePixelRatio || 1, 1.5);
    canvas.width = Math.max(1, Math.round(w * dpr));
    canvas.height = Math.max(1, Math.round(h * dpr));
    points = sphere(hero ? Math.round(Math.max(220, Math.min(600, w * 1.4))) : 110);
    if (running) draw(performance.now());
    else ensure();
  });
  resize.observe(canvas);
  const view = new IntersectionObserver(([entry]) => { inView = entry.isIntersecting; ensure(); });
  view.observe(canvas);
  const onVisibility = () => { visible = document.visibilityState === "visible"; ensure(); };
  document.addEventListener("visibilitychange", onVisibility);
  const onTheme = () => { ({ light, palettes } = readPalettes()); tinted = null; settleUntil = performance.now() + SETTLE_MS; ensure(); };
  window.addEventListener("xodus:theme", onTheme);
  reduce.addEventListener("change", ensure);
  const offPulse = onCorePulse((kind) => {
    if (reduce.matches) return; // the state text already says what happened; no flare without motion
    energy = Math.min(1.5, energy + (kind === "token" ? 0.1 : 0.55));
    settleUntil = performance.now() + SETTLE_MS;
    ensure();
  });

  return {
    update() {
      changedAt = performance.now();
      settleUntil = changedAt + SETTLE_MS;
      ensure();
    },
    destroy() {
      cancelAnimationFrame(raf);
      resize.disconnect();
      view.disconnect();
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("xodus:theme", onTheme);
      reduce.removeEventListener("change", ensure);
      offPulse();
    },
  };
}

const DOT: Record<CoreState, string> = {
  idle: "text-primary", thinking: "text-primary animate-pulse", streaming: "text-primary", working: "text-primary animate-pulse",
  waiting: "text-warn", error: "text-destructive", offline: "text-muted-foreground",
};

/** The core's state in words. Announced politely when the state changes; the running chunk count is not announced. */
export function CoreStatus({ core, className }: { core: Core; className?: string }) {
  return (
    <p role="status" className={cn("flex flex-wrap items-center gap-x-2 gap-y-0.5 text-sm", className)}>
      <span aria-hidden="true" className={cn("size-2 shrink-0 rounded-full bg-current shadow-[0_0_8px_currentColor]", DOT[core.state])} />
      <span className="font-medium">{core.label}</span>
      <span className="text-muted-foreground" aria-hidden={core.state === "streaming" || undefined}>{core.detail}</span>
    </p>
  );
}

/** The animated core. Decorative to assistive technology: always pair it with <CoreStatus>, which says the same in words. */
export function CoreOrb({ state, satellites = 0, size = "hero", className }: {
  state: CoreState; satellites?: number; size?: "hero" | "compact"; className?: string;
}) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const engine = useRef<Engine | null>(null);
  const stateRef = useRef(state);
  const satellitesRef = useRef(satellites);
  stateRef.current = state;
  satellitesRef.current = satellites;
  const [animating, setAnimating] = useState(false);

  useEffect(() => {
    if (!canvas.current) return;
    engine.current = createEngine(canvas.current, () => stateRef.current, () => satellitesRef.current, size === "hero", setAnimating);
    return () => { engine.current?.destroy(); engine.current = null; };
  }, [size]);

  useEffect(() => { engine.current?.update(); }, [state, satellites]);

  return (
    <div className={cn("relative", className)} data-core-state={state} data-animating={animating ? "true" : "false"}>
      <canvas ref={canvas} aria-hidden="true" className="absolute inset-0 size-full" />
    </div>
  );
}
