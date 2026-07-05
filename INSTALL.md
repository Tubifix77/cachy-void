# Cachy-Void-Update Installation & Provisioning Manual

Cachy-Void is a performance overlay for stock Void Linux: CachyOS-style tuning
(hardware-targeted `-march`/`-O3` userland, a BORE-patched low-latency kernel,
aggressive sysctl/zram/udev) applied **without** giving up Void's fortes — runit
stays PID 1, no systemd, and XBPS dependency resolution is left clean. A Python
update engine keeps a small allowlist of packages locally compiled and, when you
opt in, regenerates and boot-tests a custom `linux-cachy` kernel.

This manual covers the automated path (recommended) and the manual equivalent,
and documents exactly what lands on your system.

> The authoritative design lives in [`architecture.md`](architecture.md); this
> document is the operational how-to.

---

## 1. System Prerequisites & Dependencies

The host needs a build toolchain to compile source packages with `xbps-src` and
`xtools` to introspect the running system. Install them as root:

```bash
sudo xbps-install -Sy git xtools base-devel
```

Notes:

- `git` — to clone `void-packages` and this repository.
- `xtools` — provides `xcheckrestart`, `xgensum`, `vkpurge`, etc.
- `base-devel` — the compiler/toolchain metapackage `xbps-src` builds against.

There is **no** `xbps-src` package — `xbps-src` is a script that ships *inside*
the `void-packages` checkout (set up in §3). Trust verification of the BORE patch
uses SHA-256 pinning (`bore.lock`, §6), so no separate signature tool is needed.

---

## 2. Quick Start (Automated Bootstrap)

The fastest, least error-prone route is [`bootstrap.sh`](bootstrap.sh). Run it as
**your regular user** (not root) — it escalates with `sudo` only where required:

```bash
git clone https://github.com/Tubifix77/cachy-void.git
cd cachy-void
./bootstrap.sh
```

`bootstrap.sh` performs the whole provisioning end to end:

1. Verifies you are a non-root user with working `sudo`.
2. Derives the kernel tracking state from `uname -r` (e.g. running `6.12.34_1`
   ⇒ tracked series `linux6.12`, known-good kernel `6.12.34_1`).
3. Installs the prerequisites from §1.
4. Ensures a `void-packages` checkout at `~/void-packages` (clones it and runs
   `./xbps-src binary-bootstrap` if missing).
5. Runs `deploy.sh --with-grub` (see §4) and seeds the initial
   `kernel-state.json` matrix.

Override the checkout location with an environment variable:

```bash
VOID_PACKAGES=/srv/void-packages ./bootstrap.sh
```

When it finishes, skip to §7 (First Update).

---

## 3. Obtaining void-packages (manual)

If you did not use `bootstrap.sh`, set up `void-packages` yourself:

```bash
git clone https://github.com/void-linux/void-packages.git ~/void-packages
cd ~/void-packages
./xbps-src binary-bootstrap        # run as a NON-root user
```

`xbps-src` refuses to run as root; use an unprivileged user that owns the
checkout.

---

## 4. System Provisioning (`deploy.sh`)

[`deploy.sh`](deploy.sh) installs the static configuration, mirrors the Python
engine into the system, provisions the runit services, and (with `--with-grub`)
performs the sanctioned bootloader edits. Run it as **root**:

```bash
sudo ./deploy.sh --with-grub --user "$USER" --void-packages ~/void-packages
```

Key flags:

| Flag | Purpose |
|---|---|
| `--user NAME` | The unprivileged updater user (gets the sudoers grants). |
| `--void-packages DIR` | Path to your `void-packages` checkout. |
| `--with-grub` | Adds `usbcore.autosuspend=-1` to the kernel cmdline **and** sets `GRUB_DEFAULT=saved` (required for one-shot kernel boot-testing). |
| `--march ARCH` | Compiler ABI level (default `x86-64-v3`; use `x86-64-v4` only on full-AVX-512 CPUs such as Zen 4/5). |
| `--jobs N` | Build parallelism (default: `nproc`). |
| `--simulate` | Lay down files but skip runit service enablement (auto-enabled on WSL2/virtualized hosts). |
| `--dry-run` | Print planned actions without changing anything. |
| `--uninstall` | Revert every change (see §9). |

`deploy.sh` is idempotent and fully reversible: every change is recorded in a
manifest, pre-existing files are backed up before being replaced, and the
sudoers fragment is validated with `visudo -c` before it is ever activated.

---

## 5. What Gets Installed

| Path | Contents |
|---|---|
| `/usr/libexec/cachy-void-updater/` | Mirrored Python engine (`cachy_void_update.py`, `engine/`, `bore.lock`). |
| `/etc/cachy-void/updater.toml` | Updater configuration (you create this — §6). |
| `/etc/cachy-void/cachy-fragment.config` | Kernel `.config` fragment (BORE, 1000 Hz, full preempt…). |
| `/var/lib/cachy-void/kernel/kernel-state.json` | Kernel tracking + boot-test state machine. |
| `/etc/xbps.d/00-cachy-overlay.conf` | Registers the local optimized repo ahead of the mirror. |
| `<void-packages>/etc/conf` | Compiler profile (`-march=… -O3 -pipe`, ccache). |
| `/etc/sysctl.d/99-cachy-gaming.conf` | Gaming sysctl profile. |
| `/etc/udev/rules.d/60-ioschedulers.rules` | Per-medium I/O schedulers. |
| `/etc/modprobe.d/99-gaming-input.conf`, `/etc/modules-load.d/cachy.conf` | Input polling + BBR module. |
| `/etc/sudoers.d/cachy-void` | Narrow NOPASSWD grants for the updater user. |
| `/etc/sv/zramen/`, `/etc/sv/cachy-health/` | runit services (zram swap; post-boot health daemon). |
| `/var/log/cachy-health/` | Health daemon logs (svlogd). |

---

## 6. Post-Install Configuration

### 6.1 Updater configuration — `/etc/cachy-void/updater.toml`

Create it (adjust `void_packages` and the target allowlist):

```toml
[paths]
void_packages = "/home/YOURUSER/void-packages"

[build]
jobs = 0        # 0 = nproc

[packages]
# The ONLY packages compiled locally. Seed set:
targets = ["linux-cachy", "mesa", "vulkan-loader", "SDL2",
           "pipewire", "wireplumber", "wine", "gamemode", "mangohud", "ffmpeg"]
# Never build these (bootstrap layer); comes from the upstream mirror:
blacklist = ["glibc", "musl", "gcc", "binutils", "xbps", "runit", "base-files"]

[services]
restart_skip = ["udevd", "dbus", "elogind"]

[kernel]
enable = true
```

### 6.2 BORE patch trust anchor — `bore.lock`

Before the first `linux-cachy` build, pin the BORE patch you trust. Edit the
mirrored lockfile `/usr/libexec/cachy-void-updater/bore.lock`: set
`pinned_commit`, the per-series `sha256`, and stamp `approved`. The engine
verifies the fetched patch against this hash and refuses to build on a mismatch —
it never edits the lockfile for you (trust-on-first-use is a human act).

### 6.3 Kernel tracking state

`bootstrap.sh` seeds `/var/lib/cachy-void/kernel/kernel-state.json` for you. If
you provisioned manually, set at least `base_series` (e.g. `6.12`) and a
`known_good.kver` matching your current `uname -r`; leave `ported_version` low
(e.g. `0.0.0_0`) so the first commit builds `linux-cachy`.

---

## 7. First Update

Preview the queue (read-only — safe any time):

```bash
/usr/libexec/cachy-void-updater/cachy_void_update.py --check --config /etc/cachy-void/updater.toml
```

Sync `void-packages` to upstream, then build + deploy:

```bash
# as the updater user
cachy() { /usr/libexec/cachy-void-updater/cachy_void_update.py --config /etc/cachy-void/updater.toml "$@"; }

cachy --sync            # Stage 1: rebase void-packages onto upstream master
cachy --commit --yes    # Stages 3-4: build the queue, deploy, stage the kernel
```

If `linux-cachy` was built, the updater stages it for a **one-shot trial boot**
(the default stays pinned to your known-good kernel) and prints a reboot notice.
Reboot when convenient; a panic/hang simply returns to the known-good kernel.

---

## 8. Command Reference

All actions take `--config PATH` (default `/etc/cachy-void/updater.toml`) and are
mutually exclusive:

| Action | Meaning |
|---|---|
| `--sync` | Rebase `void-packages` onto upstream `master`, rolling back on conflict. |
| `--check` | Compute and print the build/deploy queue. Read-only. |
| `--commit` | Build the queue, deploy it, run the G2 config gate, and stage a queued kernel. |
| `--rollback` | Re-pin the GRUB default to the known-good kernel. |
| `--health-daemon` | Run the post-boot health watchdog loop (used by the `cachy-health` service). |

Modifiers: `--dry-run` (plan only), `--yes` (unattended, no confirmation prompt).

The `cachy-health` runit service runs the daemon automatically after boot; on a
virtualized/WSL host it detects the sandbox, logs metrics, and makes no
supervisor or GRUB changes.

---

## 9. Uninstalling

`deploy.sh` reverts everything it did — restores backed-up files, removes the
mirrored engine and services, and removes only the packages it installed:

```bash
sudo ./deploy.sh --uninstall
```

Runtime kernel parameters applied via sysctl persist until the next reboot.

---

## 10. Notes on Virtualized / WSL2 Hosts

Hardware-level components (the custom kernel, physical udev, GRUB one-shot
boot-testing) cannot be exercised without real hardware and a real runit PID 1.
On WSL2/virtualized profiles `deploy.sh` auto-enables `--simulate` and the health
daemon degrades to logging-only, so the software engine, compiler pipeline, and
script logic can be validated safely, but kernel staging is a no-op there.
