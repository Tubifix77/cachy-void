# Cachy-Void — Branding & Desktop Identity

> **Status: IMPLEMENTED.** This design spec is now shipped as the `cachy-branding`
> applier (`system/bin/cachy-branding`) + theme assets (`system/branding/`,
> `assets/wallpapers/`), installed opt-in by `deploy.sh --with-branding` and applied
> per-user by running `cachy-branding` (see INSTALL.md §14). First applied live on the
> LXQt/nvidia470 testbed, 2026-07-18. Everything here stays **opt-in and DE-aware**
> (see `architecture.md` §0 and `future-ideas.md` §2/§6): a user's existing look is
> never overridden, and `cachy-branding --remove` restores it. Reference target is
> **LXQt on X11**; Wayland notes are flagged for future modern-GPU targets.
> Authoritative implementation spec remains `architecture.md` — this is the *look*.

---

## 1. Aesthetic direction — low-key, matte, restrained

Cachy-Void fuses two philosophies:

- **Void** — systems-first minimalism: honest, transparent, flat, imposes no identity, quiet.
- **CachyOS** — a performance ethos: tuned, instant, purposeful.

**The identity is the fusion itself** — Void's honest, quiet, minimal *form* carrying
CachyOS's tuned, fast, purposeful *substance*. The look must read as **both at once**:
calm *and* high-performance. Restraint is Void's contribution; the feel of a
finely-tuned instrument is CachyOS's — and neither wins at the other's expense (pure
minimalism would lose the performance soul; anything flashy would betray Void). Note
*how* we express performance: the Void way — not RGB and gauges, but **honest, legible
telemetry** (btop-style) — a fast machine you can see straight into.

The target feel is **low-key and modern** — quiet, matte, understated. The palette
(charcoal + muted army-green + oxblood) lends a calm, low-visibility character, like an
instrument panel dimmed for night use — not a gamer rig.

> **On the word "grunge":** it surfaced earlier only as an *analogy for energy level* —
> grunge is to punk what this is meant to be to something louder: the calmed-down,
> restrained member of the same family. It is **not** a call for a literal grunge *look*.
> Cachy-Void is **not** grungy — no distressed, worn, torn, or decayed textures, no dirt,
> no noise. It stays clean, flat, and quiet. **Restraint, not grunge, is the thesis.**

**Design rules (the tie-breakers when a choice is unclear):**

1. **Flat over skeuomorphic** — no fake 3D buttons, no glass/blur.
2. **Matte over glossy** — tight, dark shadows for depth, never glowing halos.
3. **Instant over animated** — snappy, near-zero transitions; motion is feedback, not decoration.
4. **Information over ornament** — text and density (à la `btop`) beat dials and gauges.
5. **One restrained accent** — the green appears *sparingly*; a highlight, not a flood. Zero "gaming RGB."
6. **Quiet by default** — when in doubt, do less.

---

## 2. Canonical palette

Single source of truth (mirrors `future-ideas.md` §6b). Everything else derives from these.

| Role | Token | Hex | RGB | CMYK | HSL |
|---|---|---|---|---|---|
| Background — obsidian | `--bg` | `#1b1d1e` | 27, 29, 30 | 10, 3, 0, 88 | 200°, 5%, 11% |
| Panel/border — graphite | `--panel` | `#282c34` | 40, 44, 52 | 23, 15, 0, 80 | 220°, 13%, 18% |
| Foreground — grey-white | `--fg` | `#abb2bf` | 171, 178, 191 | 10, 7, 0, 25 | 219°, 14%, 71% |
| Accent — "the Void" green | `--accent` | `#478061` | 71, 128, 97 | 45, 0, 24, 50 | 147°, 29%, 39% |
| Alert — oxblood | `--alert` | `#8a2f32` | 138, 47, 50 | 0, 66, 64, 46 | 358°, 49%, 36% |

**Derived tokens:**

- `--fg-dim` (inactive/disabled text): `#5c6370` — the One-Dark "comment" grey; cool, low-sat, reads clearly muted.
- `--on-accent` (text placed *on* the green): `--bg` `#1b1d1e` — maximum contrast.

The palette is deliberately tiny. **Resist adding hues.** If a "warning" tone is ever
truly needed, derive a *muted* amber — never a bright one — and add it here first.

**Contrast / usage rules (keeps it readable *and* on-brand):**

- `--fg` on `--bg` ≈ 9:1 → safe for all body text.
- `--accent` (green) is mid-dark: use it for **selection fills (with `--on-accent` text),
  borders, active indicators, and labels** — *not* small body text on `--bg` (~3:1, too low).
- `--alert` (oxblood) is dark: use as a **fill/indicator/border with light text on it** —
  never as small text on `--bg`.

---

## 3. Typography

- **JetBrains Mono** (primary) or **IBM Plex Mono** — engineered, technical, monospace;
  reinforces the "instrument panel" feel. They read as *drawn by an engineer, not a designer*.
- Flat rendering — no drop-shadowed or outlined text.
- Void package: **verify** the exact name (`xbps-query -Rs jetbrains`) before use — likely
  `font-jetbrains-mono` or a nerd-fonts variant.

---

## 4. Motion & depth

- Compositor shadows: **small radius, tight offset, ~0.6 opacity, dark** — a thin
  separation from the backdrop, not a halo.
- Fades: **fast** (~0.08 step) or off. No glassy cross-fades.
- No wobble / genie / cube effects — the opposite of the ethos.

---

## 5. Component plan (curated through the philosophy)

**Curation first.** From the wider "desktop pimping" menu, the philosophy *rejects* the flashy ones:

| Tool | Verdict | Why |
|---|---|---|
| **Tint2** | ✅ keep (panel/dock) | razor-thin, text/icon, ~0 MB, fully text-configured — peak Void. |
| **Plank** | ✅ ok (dock) | fine *if kept flat/matte* (theme below); simplest starting dock. |
| **Cairo-Dock** | ❌ reject | 3D physics, reflections, macOS animation — the exact flash we avoid. |
| **Rofi** | ✅ keep (launcher) | keyboard-first Spotlight overlay; fully themable to palette. |
| **Picom** | ✅ keep (compositor, X11) | tight matte shadows; correct for this X11/nvidia470 box. |
| **Kvantum** | ✅ keep (Qt engine) | LXQt is Qt — this is where most of the "skin" lives. |
| **Conky** | ⚠️ keep, *minimal/text only* | honest telemetry is on-brand; gauges/dials are not. |
| **Variety** | ❌ skip | auto-rotating internet wallpapers fight a *fixed* identity. |
| **Feh** | ✅ keep | set one curated static wallpaper, programmatically. |

### 5.1 Kvantum — the core Qt skin (biggest single win for LXQt)

`~/.config/Kvantum/void-tactical/void-tactical.kvconfig`:

```ini
[%General]
comment=Cachy-Void low-key
version=1.0

[GeneralColors]
window.color=#1b1d1e
base.color=#1b1d1e
button.color=#282c34
window.text.color=#abb2bf
text.color=#abb2bf
button.text.color=#abb2bf
highlight.color=#478061
highlight.text.color=#1b1d1e
link.color=#478061
inactive.window.text.color=#5c6370
inactive.text.color=#5c6370
tooltip.color=#8a2f32
tooltip.text.color=#abb2bf

[Hacks]
# clean, instant-response feel
respect_darkness=true
```

**The SVG reality (the part drafts gloss over):** Kvantum draws widget *shapes* from an
SVG that sits next to the `.kvconfig`. Do **not** hand-draw one. Copy an existing dark
theme's SVG (e.g. `KvGnomeDark.svg`) into the folder as `void-tactical.svg`, keep its
shapes, and let the `.kvconfig` recolor everything. Apply via `kvantummanager`, then
LXQt → Appearance → Widget Style → **Kvantum**.

### 5.2 Rofi — launcher (`~/.config/rofi/config.rasi`)

```css
* {
    bg:     #1b1d1e;
    panel:  #282c34;
    fg:     #abb2bf;
    accent: #478061;
    background-color: transparent;
    text-color: @fg;
    font: "JetBrains Mono 11";
}
window   { background-color: @bg; border: 2px; border-color: @panel; width: 600px; padding: 15px; }
mainbox  { children: [ inputbar, listview ]; spacing: 10px; }
inputbar { background-color: @panel; padding: 10px; children: [ prompt, entry ]; }
prompt   { text-color: @accent; margin: 0 10px 0 0; }
listview { lines: 8; scrollbar: false; spacing: 5px; }
element  { padding: 8px; }
element selected { background-color: @accent; text-color: @bg; }
```

### 5.3 Picom — compositor, X11 (`~/.config/picom/picom.conf`)

```ini
# tight, dark, industrial shadows — no glassy halos
shadow = true;
shadow-radius = 5;
shadow-opacity = 0.6;
shadow-offset-x = -5;
shadow-offset-y = -5;
# snappy, instant — no glass fade
fading = true;
fade-in-step = 0.08;
fade-out-step = 0.08;
```

### 5.4 Plank — dock, if used (`~/.local/share/plank/themes/void-tactical/dock.theme`)

```ini
[PlankTheme]
CornerRadius=4                       # low = sharp/tactical
[PlankDockTheme]
FillStartColor=27;;29;;30;;242       # obsidian @ ~95%
FillEndColor=27;;29;;30;;242
OuterStrokeColor=40;;44;;52;;255     # thin graphite line
InnerStrokeColor=0;;0;;0;;0
ActiveIndicatorColor=71;;128;;97;;255   # Void green
UrgentGlowColor=138;;47;;50;;255        # oxblood
```
*(Verify section/key names against the installed Plank version — they vary; the color
format is `R;;G;;B;;A`, 0–255.)*

### 5.5 Conky — minimal telemetry (text only, **no gauges**)

A thin monospace column on the wallpaper — honest numbers, not a dashboard. Sketch
(tailor the sensor lines to the box's real `hwmon`/`sensors`):

```lua
conky.config = {
    alignment='top_right', gap_x=24, gap_y=48, minimum_width=210,
    own_window=true, own_window_type='desktop',
    own_window_argb_visual=true, own_window_transparent=true,
    double_buffer=true, update_interval=2.0, draw_shades=false,
    font='JetBrains Mono:size=9',
    default_color='abb2bf', color1='478061', color2='8a2f32',
}
conky.text = [[
${color1}CACHY-VOID${color}  ${nodename}
${color1}kern ${color}${kernel}
${color1}cpu  ${color}${cpu}%    ${color1}mem ${color}${memperc}%
${color1}net  ${color}${downspeed} v  ${upspeed} ^
${color1}up   ${color}${uptime_short}
]]
```
No bars, no rings, no meters — density through plain text is the point. If
`nvidia-smi` is present, `cachy-branding` adds a `gpu temp / util` line straight
from the chip (`--query-gpu=temperature.gpu,utilization.gpu`) — legacy Optimus/
PRIME render-offload makes in-game overlays (MangoHud/NVML) misreport load, but
a direct `nvidia-smi` query holds up fine; the line is polled every 5s (`execi 5`),
not every redraw. On multi-monitor "detached TV" setups where the real output
isn't negotiated yet at login, Conky's autostart is delayed ~8s so its
`top_right` anchor computes against the right screen, not a stale default.

### 5.6 The mark & wallpaper

**Canonical logo set** (SVGs in [`assets/`](assets/) — dark-surface: grey/green on a
transparent field; for light surfaces swap the greys for the dark tokens):

| Asset | Role | Contents |
|---|---|---|
| `assets/void-cachy-mark.svg` | **primary lockup** | landscape 3-frame core + wordmark — headers, docs, splash |
| `assets/void-cachy-icon.svg` | **primary icon** | the bare green core diamond — favicon, LXQt panel button |
| `assets/void-cachy-square.svg` | **alt icon / badge** (back-pocket) | concentric-square frames + core — square icon slots, stickers, centred badges |

Rules of use: the **landscape lockup is primary** (it flows beside the wordmark); the
**square is the back-pocket alt** wherever a square shape fits better; the **bare core is
the tiny icon** everywhere (the frames don't survive to favicon size). Exactly one green
element — the mark *is* the "one restrained accent."

Fixed brand colours (NOT theme-adaptive): outer frame `#abb2bf`, inner frames `#5c6370`,
core + tech line `#478061`, wordmark `#abb2bf`, tagline `#5c6370`. Do **not** use
`#282c34` for text or inner frames on obsidian — at ~1.2:1 it disappears.

**Terminal / fastfetch** (no SVG in a TTY) — the ASCII lockup (colour the `◆` core
`#478061` in the fastfetch config, everything else `#abb2bf`/`#5c6370`):

```
╭─────────────╮
│ ╭─────────╮ │
│ │    ◆    │ │   cachy · void
│ ╰─────────╯ │   performance core
╰─────────────╯   runit · xbps · x86-64-v3 · BORE
```

One-line nameplate (prompt or panel header):
`◆ cachy·void ── performance core ── runit · xbps · v3 · BORE`

**Wallpaper set** (shipped in [`assets/wallpapers/`](assets/wallpapers/) as clean SVGs —
crisp at any resolution, locked to the palette; render to PNG with `rsvg-convert`/ImageMagick):

| File | Motif | Feel |
|---|---|---|
| `cachy-void-official.svg` | **flagship** — concentric squares + rotated diamond, green core with a *void* at its centre, schematic corner annotation | engineered, the identity piece |
| `void-cachy-radar.svg` | concentric circles (Void's circular logo), "performance pulse" | calm, restful |
| `void-cachy-hex.svg` | concentric hexagons (Cachy's hexagon logo) | Cachy-forward |
| `void-cachy-flow.svg` | turned-down green ambient + topographic ridges | soft, atmospheric |

All are matte, on-palette, low-contrast, never busy — obsidian field, exactly one green
accent. The green core carries a tiny obsidian **void** at its centre: Void *inside* Cachy,
the fusion made literal. Set with `feh --bg-fill`, or on LXQt via `pcmanfm-qt
--set-wallpaper=<png> --wallpaper-mode=stretch`. Default = the flagship.

### 5.7 Desktop composition — molding it together

The rule for the *whole* desktop is the same as for a single widget: **quiet by default,
one thing doing each job.** Do not stack a panel *and* a dock that both show a task list —
pick one primary launcher/switcher and let everything else recede.

**Recommended LXQt layout (played-down):**

- **One panel, themed** — the LXQt panel is the anchor: menu button (the mark),
  task list, tray, clock. Kept thin and dark (the `void-tactical` LXQt theme). This alone
  is a complete, honest desktop — the most Void choice. `cachy-branding` edits the
  user's *existing* `panel.conf` in place (position/size/plugin list only) rather
  than fabricating one from scratch — a from-scratch `[panel1]`/`[mainmenu]` rewrite
  was tried once and shipped a menu button that silently failed to render. The
  `mainmenu` icon is swapped 1:1 for the diamond mark at the same size the theme
  already used (no icon= reveals a rendering bug in that plugin, so the safest
  form is: edit what's there, don't replace the section).
- **A dock is *optional*, and must stay subtle.** If a dock is wanted, add **Plank** as a
  *minimal launcher only* — not a showpiece:
  - Flat/matte theme (§5.4): sharp corners, thin graphite stroke, obsidian fill, green
    active-indicator. **No** glass, **no** macOS zoom/bounce (`zoom-enabled=false`).
  - Small icons (~40 px), **auto-hide or dodge-windows** so it never competes with content
    or the wallpaper.
  - Autostart delayed ~8s on multi-monitor "detached TV" setups — Plank docking
    against whatever output is live at that instant means it can end up parked
    near the old default (laptop-panel) screen center until the next restart if
    the real display hasn't been negotiated yet.
  - Position it so it does **not** fight the panel: either move the LXQt panel to a thin
    **top** bar (tray/clock/menu) and give Plank the **bottom** for app launching, **or**
    keep the bottom panel and dodge-hide Plank on a **side** edge. Never two bars on the
    same edge.
  - Curation still applies (§5): **Cairo-Dock is rejected** (3D/reflections/animation);
    Plank *only if* kept flat. Tint2 is the even-more-Void alternative (razor-thin, text).
- **Compositor** — Picom (§5.3) for tight matte shadows; it makes windows sit on the
  backdrop without any glow. Already the correct, low-key depth cue.
- **Launcher** — Rofi (§5.2) is keyboard-first and overlays nothing permanently; bind it to
  `Super+Space` rather than adding another visible bar.
- **Menu key** — the Windows/**Super** key opens the panel menu, bound through LXQt's own
  global-shortcut daemon: `[Super_L.N]` in `globalkeyshortcuts.conf` with
  `path=/panel/<menu>/show_hide` (the plugin is `fancymenu` on newer LXQt, `mainmenu` on
  older — `cachy-branding` detects which). Current LXQt binds a bare `Super_L` cleanly, so
  no xcape/xdotool bridge is needed. The daemon rewrites that file on exit, so the applier
  stops it, edits, then restarts it — and skips the edit entirely if a `Super_L` binding
  already exists (never clobbers a user's own).
- **Telemetry** — Conky (§5.5), if used, is text-only, top-right, on the wallpaper — honest
  numbers, never gauges.

**The played-down test:** on a fresh boot with no windows open, the desktop should read as
*mostly wallpaper* — a calm obsidian field with the mark — with at most one thin bar. If two
elements are both asking for attention, remove or auto-hide one.

---

## 6. Scope & rules

- **Opt-in overlay, never forced** — ship as config files the user *chooses* to apply (or
  a future installer toggle). Void imposes nothing; neither do we.
- **DE-aware** (future-ideas §2): detect the DE; these configs assume **LXQt / Qt / X11**.
  On a non-matching setup, fall back to just wallpaper + palette.
- **GRUB theming: out of scope** on the foreign-GRUB testbed (another distro owns boot).
- **Reversible** — a Cachy-Void that leaves no scars (the deploy-ledger ethos).

---

## 7. Before "in practice" — verify / open questions

- Confirm Void package names on the box (`xbps-query -Rs`): `rofi`, `plank`, `tint2`,
  `picom`, `conky`, `kvantum` (+ `qt5ct`/`qt6ct`), a JetBrains Mono font, `feh`. **Don't
  trust names blind** — see [[spec-bug-game-devices-udev]].
- Pick the Kvantum base-SVG theme to fork + recolor.
- Source or create the wallpaper asset (dark, structural, on-palette).
- Tailor the Conky sensor lines to the Medion's real sensors.
- **X11 today** (nvidia470). If a target ever runs a modern GPU + Wayland: revisit the
  compositor (picom → native), `rofi` → `rofi-wayland`, and Conky (X11-bound).
