# Roadmap: GUI, Plasma Theme, and Smart Detection

This document summarizes the concepts and features planned for our Cachy-fied
Void Linux installer/updater in the next phase of development.

> Status: **ideas / not yet implemented.** Nothing here is built. It captures
> intent for a future run; the authoritative design for what *is* implemented
> remains [`architecture.md`](architecture.md).

---

## 1. KDE Plasma as the Locked-In Gaming Target

We have decided to target all graphical optimizations and system integrations
specifically toward **KDE Plasma (Wayland)**. This ensures:

- Full utilization of **NVIDIA Explicit Sync** on the GTX 750 Ti graphics card.
- Perfect **fractional scaling** on the living room TV.
- Smooth **controller navigation** and a console-like experience when launching
  Steam Big Picture.

---

## 2. Smart Environment Detection (DE Check)

To respect Void's philosophy of minimal intervention (never forcing unnecessary
configurations on a user), we will build an intelligent detection scanner into
our Python program.

**How it works:** the program scans the system for the presence of KDE Plasma
files (e.g. checking whether `/usr/bin/plasmashell` or `kwin` exists).

- **If Plasma IS installed:** the user is prompted at the end of installation to
  inject our custom hybrid Void/Cachy theme — SDDM login screen, terminal
  configuration, hybrid color palette, and desktop shortcuts.
- **If Plasma is NOT installed:**
  - The program cleanly hides all Plasma-related theming options.
  - Instead, it offers a lightweight fallback: place a sharp, high-quality static
    hybrid-themed wallpaper in `/usr/share/backgrounds/` and leave the user's
    existing minimalist environment (e.g. LXQt or i3) completely untouched.

---

## 3. Graphical Installer GUI

We will design a dedicated, visually polished frontend for the installer that
bridges the design identities of both distributions.

**Aesthetics:** a sleek, deep carbon-gray background (representing Void Linux)
highlighted by glowing neon-green (Void) and vibrant electric cyan/teal accents
(CachyOS).

**Typography:** a sharp, modern sans-serif font for buttons and settings,
combined with a clean, highly legible monospace font for live compilation logs
and terminal outputs.

**Key features:**

- Interactive checkboxes to toggle optional system components (Mesa, Pipewire,
  pro-audio setups, etc.).
- A real-time, accurate compilation progress bar.
- A clear visual dashboard showing the active boot status (`kernel-state.json`),
  displaying the currently active "known good" kernel and any staged one-shot
  test kernels.

---

## 4. Development and Testing Workflow

We will continue using the **WSL Void environment** on the laptop as our primary
safe sandbox:

- All GUI components, detection scripts, and JSON parser updates will be built
  and thoroughly debugged in WSL first, to eliminate any risk to physical
  partitions.
- Once the code passes all tests and is 100% stable, we will push the changes to
  GitHub, pull them down to the **Asus GR8 mini-PC** in the living room, and
  deploy the physical kernel compilation and system adjustments directly to bare
  metal.
