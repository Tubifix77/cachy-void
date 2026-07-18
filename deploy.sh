#!/usr/bin/env bash
#
# deploy.sh — Cachy-Void system installer
# Installs the static system configuration (§1, §3, §4), mirrors the Python
# updater engine into /usr/libexec/cachy-void-updater, provisions the runit
# services (§3.2 zram, §8.7 cachy-health), and can revert everything to stock.
#
# Design guarantees:
#   * Idempotent  — safe to re-run; managed files are refreshed in place and
#                   originals are backed up exactly once.
#   * Ledger      — every change is recorded in a tagged, timestamped manifest
#                   ON THE TARGET SYSTEM (/var/lib/cachy-void/deploy.manifest).
#                   View it with --log. Roll back everything (--uninstall), one
#                   route's changes (--uninstall-tag test), or a single item
#                   (--uninstall-item PATH). Backups are restored on rollback.
#   * Rescueable  — --root DIR operates on a MOUNTED Void tree from another
#                   distro (e.g. dual-boot Debian), so a Void that no longer
#                   boots can still be inspected and rolled back over SSH.
#                   Package steps are skipped offline (see INSTALL.md §11).
#   * Fail-safe   — the sudoers fragment is validated with `visudo -c` before it
#                   is ever activated; a bad rule can never lock out sudo.
#   * Sandbox-safe — WSL2/virtualized profiles auto-enable --simulate: files are
#                   laid down but runit service enablement is skipped.
#
# Usage:
#   sudo ./deploy.sh [--user NAME] [--void-packages DIR] [--march ARCH]
#                    [--jobs N] [--with-grub] [--with-schedule] [--with-branding]
#                    [--hud-profile auto|full|minimal]
#                    [--tag core|test|opt] [--simulate] [--dry-run] [--root DIR]
#   sudo ./deploy.sh --log                 [--root DIR]
#   sudo ./deploy.sh --uninstall           [--dry-run] [--root DIR]
#   sudo ./deploy.sh --uninstall-tag NAME  [--dry-run] [--root DIR]
#   sudo ./deploy.sh --uninstall-item PATH [--dry-run] [--root DIR]
#
# With --root, every path argument (and every path in the ledger) is the
# LOGICAL in-Void path; the DIR prefix is applied only when touching the disk.
#
# Run `./deploy.sh --help` for this text.

set -euo pipefail

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
readonly SRC_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SYS_DIR="$SRC_DIR/system"
readonly STATE_DIR="/var/lib/cachy-void"          # logical (in-Void) path
readonly TS="$(date +%Y%m%d%H%M%S)"
readonly TAB=$'\t'

# Packages this deliverable installs (§3.2 zram; §4.7 xtools provides
# xcheckrestart, needed by the updater's service-cycling stage). Game
# controllers need no package on Void — the kernel + elogind seat uaccess
# handle standard pads, and Steam ships its own rules (§3.3); there is no
# game-devices-udev package.
readonly PKG_ZRAM="zramen"
readonly PKG_XTOOLS="xtools"
readonly PKG_SNOOZE="snooze"   # §4.9: the scheduled-update runit service execs it
# §3.4 gaming userspace layer.
readonly PKG_GAMEMODE="gamemode"
readonly PKG_MANGOHUD="MangoHud"           # case-sensitive; lowercase does not exist
readonly PKG_MANGOHUD32="MangoHud-32bit"   # 32-bit titles; multilib-gated (optional)
readonly PKG_XZ="xz"                        # cachy-proton extracts .tar.xz releases
readonly CACHY_GAME_WRAPPER="/usr/local/bin/cachy-game"
readonly CACHY_PROTON_HELPER="/usr/local/bin/cachy-proton"
readonly MANGOHUD_CONF="/etc/xdg/MangoHud/MangoHud.conf"
# §branding: void-tactical LXQt desktop (opt-in, --with-branding). The applier
# runs per-user (cachy-branding); deploy.sh only installs packages + assets.
readonly PKG_BRANDING="kvantum papirus-icon-theme papirus-folders plank rofi conky picom python3-PyQt5"
readonly CACHY_UPDATER_GUI="/usr/local/bin/cachy-updater-gui"
readonly BRANDING_ASSETS="/usr/share/cachy-void/branding"
readonly CACHY_BRANDING_BIN="/usr/local/bin/cachy-branding"

# Fixed install location for the mirrored Python engine (§6/§8.9).
readonly CACHY_ENGINE="/usr/libexec/cachy-void-updater"
readonly HEALTH_LOG_DIR="/var/log/cachy-health"
readonly SCHED_LOG_DIR="/var/log/cachy-void-update"   # §4.9 scheduled-run logs
readonly SNAP_DIR_DEFAULT="/.cachy-snapshots"          # §9.5 default snapshot subvol

# ---------------------------------------------------------------------------
# Options (mutable)
# ---------------------------------------------------------------------------
DO_UNINSTALL=false
DO_LOG=false
DRY_RUN=false
WITH_GRUB=false
WITH_SCHEDULE=false       # §4.9: also ENABLE the unattended cachy-void-update timer
WITH_BRANDING=false       # branding: install the void-tactical desktop toolkit + applier
HUD_PROFILE="auto"        # §3.4 MangoHud config: auto|full|minimal (minimal = legacy Optimus)
SIMULATE=false            # WSL2/sandbox: lay down files, skip init-dependent ops
ROOT=""                   # offline mode: mounted Void tree prefix ("" = live)
DEPLOY_TAG="core"         # ledger tag for this run (core|test|opt|...)
UN_TAG=""                 # --uninstall-tag filter
UN_ITEM=""                # --uninstall-item filter
UPDATER_USER="${SUDO_USER:-}"
CHOWN_USER=""             # resolved owner for user-owned dirs (see do_install)
VOID_PACKAGES="${VOID_PACKAGES:-}"
MARCH=""                  # empty = auto-detect via the §1.2 ladder (see detect_march)
MAKEJOBS=""
MANIFEST=""               # physical manifest path; finalized in main()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
if [ -t 1 ]; then
    C_INFO=$'\033[0;36m'; C_OK=$'\033[0;32m'; C_WARN=$'\033[0;33m'
    C_ERR=$'\033[0;31m';  C_OFF=$'\033[0m'
else
    C_INFO=""; C_OK=""; C_WARN=""; C_ERR=""; C_OFF=""
fi
log()  { printf '%s==>%s %s\n'  "$C_INFO" "$C_OFF" "$*"; }
ok()   { printf '%s ok%s %s\n'  "$C_OK"   "$C_OFF" "$*"; }
warn() { printf '%swarn%s %s\n' "$C_WARN" "$C_OFF" "$*" >&2; }
die()  { printf '%serror%s %s\n' "$C_ERR" "$C_OFF" "$*" >&2; exit 1; }

usage() {
    awk 'NR<3 {next} /^set -euo/{exit} {sub(/^# ?/,""); print}' "${BASH_SOURCE[0]}"
    exit "${1:-0}"
}

# rp LOGICAL — map a logical in-Void path to the physical path we touch.
rp() { printf '%s%s' "$ROOT" "$1"; }

# live — true only when operating on the running system (not --root, not WSL).
live() { [ -z "$ROOT" ] && ! $SIMULATE; }

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --uninstall)      DO_UNINSTALL=true ;;
            --uninstall-tag)  DO_UNINSTALL=true; UN_TAG="${2:?--uninstall-tag needs a value}"; shift ;;
            --uninstall-item) DO_UNINSTALL=true; UN_ITEM="${2:?--uninstall-item needs a value}"; shift ;;
            --log)            DO_LOG=true ;;
            --tag)            DEPLOY_TAG="${2:?--tag needs a value}"; shift ;;
            --root)           ROOT="${2:?--root needs a value}"; ROOT="${ROOT%/}"; shift ;;
            --dry-run)        DRY_RUN=true ;;
            --simulate)       SIMULATE=true ;;
            --with-grub)      WITH_GRUB=true ;;
            --with-schedule)  WITH_SCHEDULE=true ;;
            --with-branding)  WITH_BRANDING=true ;;
            --hud-profile)    HUD_PROFILE="${2:?--hud-profile needs auto|full|minimal}"; shift ;;
            --user)           UPDATER_USER="${2:?--user needs a value}"; shift ;;
            --void-packages)  VOID_PACKAGES="${2:?--void-packages needs a value}"; shift ;;
            --march)          MARCH="${2:?--march needs a value}"; shift ;;
            --jobs)           MAKEJOBS="${2:?--jobs needs a value}"; shift ;;
            -h|--help)        usage 0 ;;
            *)                die "unknown argument: $1 (try --help)" ;;
        esac
        shift
    done
    if [ -n "$ROOT" ] && [ ! -d "$ROOT" ]; then
        die "--root $ROOT is not a directory (mount the Void partition first)"
    fi
}

require_root() {
    [ "$(id -u)" -eq 0 ] || die "must run as root — try: sudo $0 $*"
}

# detect_march — §1.2 ladder, mirrors engine/template.py detect_march(): the
# highest ABI level this host can PROVE wins; absence of proof degrades, never
# upgrades (v3 binaries SIGILL on pre-Haswell CPUs — real targets exist).
detect_march() {
    local f v="x86-64"
    f=" $(grep -m1 '^flags' /proc/cpuinfo 2>/dev/null | cut -d: -f2) "
    [[ "$f" == *" sse4_2 "* && "$f" == *" popcnt "* ]] && v="x86-64-v2"
    [[ "$f" == *" avx2 "* && "$f" == *" fma "* && "$f" == *" bmi2 "* ]] && v="x86-64-v3"
    [[ "$f" == *" avx512f "* && "$f" == *" avx512bw "* && "$f" == *" avx512cd "* \
       && "$f" == *" avx512dq "* && "$f" == *" avx512vl "* ]] && v="x86-64-v4"
    printf '%s' "$v"
}

# Detect a virtualized/sandbox profile with no runit PID 1 (WSL2). In that case
# we still install files but skip init-dependent operations (service enablement),
# so provisioning is exercised without destructive/pointless supervisor changes.
detect_sandbox() {
    [ -n "$ROOT" ] && return 0     # offline mode has its own gating
    $SIMULATE && { log "simulate mode: skipping init-dependent operations"; return; }
    if grep -qiE 'microsoft|wsl' /proc/version 2>/dev/null; then
        SIMULATE=true
        warn "WSL2/virtualized profile detected — enabling --simulate (no runit alterations)"
    fi
}

# detect_legacy_optimus — true on an NVIDIA Optimus laptop with a LEGACY driver
# (series <= 470, e.g. Kepler). On these the dGPU's load/power counters are not
# reliably exposed via NVML during PRIME offload (even nvidia-smi struggles), so
# MangoHud's GPU panel misleads — reading 0% while a game renders on the dGPU
# (§3.4). Such hosts get the minimal HUD (fps/cpu, no GPU sensors). Live-only:
# the hardware can't be probed offline (--root) or in a sandbox, so it says no.
detect_legacy_optimus() {
    [ -z "$ROOT" ] && ! $SIMULATE || return 1
    local ver major
    ver="$(cat /sys/module/nvidia/version 2>/dev/null)"   # e.g. 470.256.02
    [ -n "$ver" ] || return 1
    major="${ver%%.*}"
    [ "$major" -le 470 ] 2>/dev/null || return 1
    # Optimus proxy: PRIME offloader present, or an Intel/AMD iGPU beside the dGPU.
    command -v prime-run >/dev/null 2>&1 && return 0
    lspci 2>/dev/null | grep -iqE 'VGA.*(Intel|AMD|ATI)' && return 0
    return 1
}

# ---------------------------------------------------------------------------
# Ledger — the single source of truth for what we changed on the target.
# Format:  TYPE<TAB>TARGET<TAB>EXTRA<TAB>TAG<TAB>TS   (v1 lines had 3 fields;
# they parse with tag defaulting to "core"). All paths are LOGICAL in-Void
# paths, so the same ledger drives live and --root rollbacks identically.
#   FILE     dest-path          backup-path or "-"
#   SERVICE  service-name       "-"
#   PKG      package-name       "-"        (only recorded if WE installed it)
#   DIR      dir-path           "-"
#   SUBVOL   subvol-path        "-"        (btrfs; kept on uninstall if non-empty)
#   GRUB     /etc/default/grub  backup-path
# ---------------------------------------------------------------------------
manifest_has() {  # TYPE TARGET
    [ -f "$MANIFEST" ] && grep -qF -- "$1$TAB$2$TAB" "$MANIFEST"
}
manifest_add() {  # TYPE TARGET EXTRA
    manifest_has "$1" "$2" && return 0
    mkdir -p -- "$(dirname -- "$MANIFEST")"
    printf '%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$DEPLOY_TAG" "$TS" >> "$MANIFEST"
}

do_log() {
    [ -f "$MANIFEST" ] || die "no ledger at $MANIFEST — nothing has been deployed"
    log "change ledger: $MANIFEST"
    awk -F'\t' 'BEGIN { printf "%-8s %-6s %-14s %s\n", "TYPE", "TAG", "WHEN", "TARGET" }
        NF { tag=$4; ts=$5; if (tag=="") tag="core"; if (ts=="") ts="-";
             printf "%-8s %-6s %-14s %s\n", $1, tag, ts, $2 }' "$MANIFEST"
}

# ---------------------------------------------------------------------------
# Install primitives
# ---------------------------------------------------------------------------
# install_file SRC DEST MODE OWNER GROUP   (DEST is logical; ledger stays logical)
install_file() {
    local src="$1" dest="$2" mode="$3" owner="$4" group="$5" backup="-"
    local pdest; pdest="$(rp "$dest")"
    if $DRY_RUN; then log "[dry-run] install $dest  ($mode $owner:$group)"; return; fi
    mkdir -p -- "$(dirname -- "$pdest")"
    if [ -e "$pdest" ] || [ -L "$pdest" ]; then
        if manifest_has FILE "$dest"; then
            :  # already managed by us — overwrite in place, keep original backup
        else
            backup="${dest}.pre-cachy.${TS}.bak"
            cp -a -- "$pdest" "$(rp "$backup")"
            warn "backed up existing $dest -> $backup"
        fi
    fi
    install -m "$mode" -o "$owner" -g "$group" -- "$src" "$pdest"
    manifest_add FILE "$dest" "$backup"
    ok "installed $dest"
}

# render SRC DEST_TMP — substitute host-specific tokens into a config template.
# Values are always LOGICAL in-Void paths, never --root-prefixed.
render() {
    sed -e "s|@USER@|${UPDATER_USER}|g" \
        -e "s|@MARCH@|${MARCH}|g" \
        -e "s|@MAKEJOBS@|${MAKEJOBS}|g" \
        -e "s|@VOID_PACKAGES@|${VOID_PACKAGES}|g" \
        -e "s|@ENGINE@|${CACHY_ENGINE}|g" \
        -- "$1" > "$2"
}

# ensure_pkg PKG [optional] — install only if absent; record only what we added.
ensure_pkg() {
    local pkg="$1" optional="${2:-}"
    if [ -n "$ROOT" ]; then
        warn "offline mode: package '$pkg' skipped — install it from a chroot"
        warn "  (see INSTALL.md §11); files/services below are still provisioned"
        return 0
    fi
    if xbps-query -- "$pkg" >/dev/null 2>&1; then
        log "package $pkg already present — leaving as-is"
        return 0
    fi
    if ! xbps-query -R -- "$pkg" >/dev/null 2>&1; then
        if [ "$optional" = optional ]; then
            warn "package $pkg not in any repository — skipping (optional)"
            return 0
        fi
        die "required package $pkg not found in repositories"
    fi
    if $DRY_RUN; then log "[dry-run] xbps-install -Sy $pkg"; return; fi
    log "installing package $pkg"
    xbps-install -Sy -- "$pkg"
    manifest_add PKG "$pkg" "-"
    ok "installed package $pkg"
}

# enable_service NAME — live: symlink /etc/sv/NAME into /var/service; offline:
# link into /etc/runit/runsvdir/default (the persistent location the handbook
# prescribes for a system that is not currently booted).
enable_service() {
    local name="$1" svdir link
    svdir="$(rp "/etc/sv/$name")"
    if $DRY_RUN; then log "[dry-run] enable runit service $name"; return; fi
    if $SIMULATE && [ -z "$ROOT" ]; then
        log "[simulate] would enable runit service $name (no PID 1 runit here)"
        return
    fi
    if [ ! -d "$svdir" ]; then
        if [ -n "$ROOT" ]; then
            warn "service dir /etc/sv/$name absent in offline root (package skipped?)"
            warn "  enable it after booting Void: ln -s /etc/sv/$name /var/service/"
            return 0
        fi
        die "service dir /etc/sv/$name missing (is $name installed?)"
    fi
    if [ -n "$ROOT" ]; then
        mkdir -p -- "$(rp /etc/runit/runsvdir/default)"
        link="$(rp "/etc/runit/runsvdir/default/$name")"
    else
        link="/var/service/$name"
    fi
    if [ -L "$link" ]; then
        log "service $name already enabled"
    else
        ln -s -- "/etc/sv/$name" "$link"
        ok "enabled runit service $name${ROOT:+ (offline: takes effect on next Void boot)}"
    fi
    manifest_add SERVICE "$name" "-"
}

# install_dir DIR OWNER — create a tracked directory (removed on uninstall).
install_dir() {
    local dir="$1" owner="${2:-root}"
    if $DRY_RUN; then log "[dry-run] install -d -o $owner $dir"; return; fi
    install -d -o "$owner" -- "$(rp "$dir")"
    manifest_add DIR "$dir" "-"
}

# install_engine — mirror the verified Python engine into CACHY_ENGINE (§6/§8.9).
install_engine() {
    install_dir "$CACHY_ENGINE" root
    install_file "$SRC_DIR/updater/cachy_void_update.py" \
                 "$CACHY_ENGINE/cachy_void_update.py" 0755 root root
    # §8.3 trust anchor — must sit beside the engine (bore_lock_path default).
    install_file "$SRC_DIR/updater/bore.lock" "$CACHY_ENGINE/bore.lock" 0644 root root
    install_dir "$CACHY_ENGINE/engine" root
    local f base
    for f in "$SRC_DIR"/updater/engine/*.py; do
        [ -e "$f" ] || continue
        base="$(basename -- "$f")"
        install_file "$f" "$CACHY_ENGINE/engine/$base" 0644 root root
    done
    # CLI wrapper so `cachy-void-update` is on PATH — users and the GUI invoke this
    # (the scheduled service calls the engine directly and does not need it).
    if ! $DRY_RUN; then
        local wtmp; wtmp="$(mktemp)"
        printf '#!/bin/sh\nexec python3 %s/cachy_void_update.py "$@"\n' "$CACHY_ENGINE" > "$wtmp"
        install_file "$wtmp" /usr/local/bin/cachy-void-update 0755 root root
        rm -f -- "$wtmp"
    fi
}

# install_health_service — provision + enable the §8.7 cachy-health runit service.
install_health_service() {
    install_dir /etc/sv/cachy-health root      # tracked so uninstall rm -rf's it
    install_file "$SYS_DIR/sv/cachy-health/run" /etc/sv/cachy-health/run 0755 root root
    local tmp; tmp="$(mktemp)"; render "$SYS_DIR/sv/cachy-health/conf" "$tmp"
    install_file "$tmp" /etc/sv/cachy-health/conf 0644 root root
    rm -f -- "$tmp"
    install_file "$SYS_DIR/sv/cachy-health/log/run" /etc/sv/cachy-health/log/run 0755 root root
    install_dir "$HEALTH_LOG_DIR" root
    enable_service cachy-health
}

# install_schedule_service — provision the §4.9 unattended-update runit service.
# Files are ALWAYS laid down (tracked, uninstall reverts them) but the service is
# ENABLED only with --with-schedule: an unattended build+deploy is opt-in, never
# a default. Without the flag it sits ready and one `ln -s` away.
install_schedule_service() {
    install_dir /etc/sv/cachy-void-update root
    install_file "$SYS_DIR/sv/cachy-void-update/run" /etc/sv/cachy-void-update/run 0755 root root
    local tmp; tmp="$(mktemp)"; render "$SYS_DIR/sv/cachy-void-update/conf" "$tmp"
    install_file "$tmp" /etc/sv/cachy-void-update/conf 0644 root root
    rm -f -- "$tmp"
    install_file "$SYS_DIR/sv/cachy-void-update/log/run" /etc/sv/cachy-void-update/log/run 0755 root root
    install_dir "$SCHED_LOG_DIR" root
    if $WITH_SCHEDULE; then
        enable_service cachy-void-update
    else
        log "cachy-void-update provisioned but NOT enabled (pass --with-schedule for unattended §4.9 runs)"
    fi
}

# install_gaming_userspace — provision the §3.4 runtime layer: the cachy-game
# launch wrapper + a restrained MangoHud default, and ensure gamemode/MangoHud
# are present. gamemode is also an allowlist target, so the updater can later
# rebuild it -O3 and take it over; here we only guarantee it exists for the
# wrapper. MangoHud-32bit is multilib-gated, hence optional (non-fatal if absent).
install_gaming_userspace() {
    ensure_pkg "$PKG_GAMEMODE"
    ensure_pkg "$PKG_MANGOHUD"
    ensure_pkg "$PKG_MANGOHUD32" optional
    ensure_pkg "$PKG_XZ"          # cachy-proton needs xz to extract Proton-CachyOS
    install_file "$SYS_DIR/bin/cachy-game"   "$CACHY_GAME_WRAPPER"   0755 root root
    install_file "$SYS_DIR/bin/cachy-proton" "$CACHY_PROTON_HELPER"  0755 root root

    # §3.4 MangoHud config: full (GPU stats) vs minimal (legacy Optimus — the dGPU
    # sensors read a misleading 0%, so keep only the accurate fps/cpu telemetry).
    local hud="$HUD_PROFILE" hud_src
    if [ "$hud" = auto ]; then
        if detect_legacy_optimus; then hud=minimal; else hud=full; fi
    fi
    case "$hud" in
        minimal) hud_src="$SYS_DIR/xdg/MangoHud-minimal.conf"
                 log "MangoHud: MINIMAL profile (legacy Optimus — GPU sensors unreliable; keeping fps/cpu)" ;;
        full)    hud_src="$SYS_DIR/xdg/MangoHud.conf"
                 log "MangoHud: full profile (GPU stats enabled)" ;;
        *)       die "invalid --hud-profile '$HUD_PROFILE' (use auto|full|minimal)" ;;
    esac
    install_file "$hud_src" "$MANGOHUD_CONF" 0644 root root
    log "gaming layer ready — launch: cachy-game %command% | Proton-CachyOS: run 'cachy-proton' (per-user)"
}

# install_branding — the opt-in void-tactical LXQt desktop (branding.md). Installs
# the toolkit packages + mirrors the theme assets + the per-user `cachy-branding`
# applier. The look itself is applied by the USER running `cachy-branding` (LXQt
# config is per-user; deploy.sh never writes into a user's ~/.config here).
install_branding() {
    local p
    for p in $PKG_BRANDING; do ensure_pkg "$p"; done
    ensure_pkg arc-theme optional        # GTK-app coherence
    ensure_pkg font-hack optional        # the engineered mono
    ensure_pkg ImageMagick optional      # renders the login wallpaper + flat panel
    # mirror read-only theme assets (dir is ledger-tracked; uninstall rm -rf's it)
    install_dir "$BRANDING_ASSETS" root
    if ! $DRY_RUN && [ -z "$ROOT" ]; then
        local dst; dst="$(rp "$BRANDING_ASSETS")"
        cp -rf "$SYS_DIR/branding/." "$dst/"
        cp -f  "$SYS_DIR/config/picom.conf" "$dst/picom.conf"
        install -d -- "$dst/wallpapers"
        cp -f "$SRC_DIR"/assets/wallpapers/*.svg "$dst/wallpapers/" 2>/dev/null || true
        cp -f "$SRC_DIR"/assets/void-cachy-*.svg "$dst/" 2>/dev/null || true
        # icon-theme source: fetch Luv (cachy-branding desaturates it -> "Luv-Void"
        # mono at apply time). Optional/network — falls back to Papirus-Dark if absent.
        if command -v git >/dev/null 2>&1 && [ ! -f "$dst/luv/Luv/index.theme" ]; then
            rm -rf "$dst/luv"
            git clone --depth 1 https://github.com/Nitrux/luv-icon-theme.git "$dst/luv" 2>/dev/null \
                || warn "could not fetch Luv icon theme — branding falls back to Papirus-Dark"
        fi
    fi
    install_file "$SYS_DIR/bin/cachy-branding" "$CACHY_BRANDING_BIN" 0755 root root
    # graphical updater front-end (PyQt5, inherits the Kvantum theme) + its launcher
    install_file "$SYS_DIR/bin/cachy-updater-gui" "$CACHY_UPDATER_GUI" 0755 root root
    install_file "$SYS_DIR/applications/cachy-updater.desktop" \
                 /usr/share/applications/cachy-updater.desktop 0644 root root
    install_greeter        # SDDM login screen (system-level; needs root, done here)
    log "branding toolkit installed — apply the look by running (as your user): cachy-branding"
}

# install_greeter — brand the SDDM login screen (branding.md §5.8). System-level
# (root), so it lives in deploy.sh, NOT the per-user cachy-branding. Forks the
# stock `elarun` theme (like cachy-branding forks KvArcDark): swaps in the login
# wallpaper, flattens the metallic login panel to a dark tactical one, recolours
# the blue accent to brand green, and selects it via /etc/sddm.conf.d. Live-only
# (renders images); offline/--root and dry-run are skipped with a note. The theme
# dir + conf are ledger-tracked, so uninstall reverts to the stock greeter.
install_greeter() {
    if $DRY_RUN || [ -n "$ROOT" ]; then
        log "SDDM greeter branding is live-only — skipped (re-run on the booted system)"
        return 0
    fi
    local themes="/usr/share/sddm/themes" base T svg
    [ -d "$themes" ] || { log "no SDDM installed — greeter branding skipped"; return 0; }
    base=elarun; [ -d "$themes/$base" ] || base=maldives
    [ -d "$themes/$base" ] || { warn "no SDDM base theme (elarun/maldives) — greeter branding skipped"; return 0; }
    svg="$(rp "$BRANDING_ASSETS")/wallpapers/cachy-void-login.svg"
    [ -f "$svg" ] || { warn "login wallpaper missing at $svg — greeter branding skipped"; return 0; }

    T="$themes/void-tactical"
    install_dir "$T" root                 # tracked → uninstall rm -rf's it
    cp -rf "$themes/$base/." "$T/"        # fork the stock theme

    # background: our login wallpaper (prefer rsvg; fall back to magick)
    if command -v rsvg-convert >/dev/null 2>&1; then
        rsvg-convert -w 1920 -h 1080 "$svg" -o "$T/background.png"
    elif command -v magick >/dev/null 2>&1; then
        magick -background none "$svg" -resize 1920x1080 "$T/background.png"
    else
        warn "no rsvg-convert/magick — greeter keeps the stock background"
    fi
    printf '[General]\nbackground=background.png\n' > "$T/theme.conf"

    # flatten elarun's metallic login panel → dark tactical panel; kill the sheen
    if command -v magick >/dev/null 2>&1 && [ -f "$T/images/rectangle.png" ]; then
        magick -size 416x262 xc:none -fill '#181a1be6' -stroke '#3a3f4b' -strokewidth 2 \
            -draw 'roundrectangle 1,1 414,260 4,4' "$T/images/rectangle.png"
        [ -f "$T/images/rectangle_overlay.png" ] && magick -size 4x4 xc:none "$T/images/rectangle_overlay.png"
    fi
    # recolour the blue accent → brand green; darken the top session/layout bar
    [ -f "$T/Main.qml" ] && {
        sed -i 's/#0b678c/#478061/Ig' "$T/Main.qml"
        sed -i 's/width: parent.width; height: 40/width: parent.width; height: 40; color: "#1b1d1e"; opacity: 0.85/' "$T/Main.qml"
    }
    [ -f "$T/metadata.desktop" ] && sed -i 's/^Name=.*/Name=Void Tactical/' "$T/metadata.desktop"

    install -d -- /etc/sddm.conf.d
    install_file "$SYS_DIR/sddm/10-cachy.conf" /etc/sddm.conf.d/10-cachy.conf 0644 root root
    log "SDDM greeter branded (void-tactical, forked from $base) — visible at next login"
}

grub_regen() {
    if [ -n "$ROOT" ]; then
        warn "offline mode: grub config not regenerated — run update-grub from"
        warn "  the booted system (or a chroot) if this box owns its bootloader"
        return 0
    fi
    if command -v update-grub >/dev/null 2>&1; then
        update-grub
    elif [ -f /boot/grub/grub.cfg ]; then
        grub-mkconfig -o /boot/grub/grub.cfg
    else
        warn "could not locate grub.cfg to regenerate — do it manually"
    fi
}

# ---------------------------------------------------------------------------
# Host-specific value resolution
# ---------------------------------------------------------------------------
resolve_void_packages() {
    [ -n "$VOID_PACKAGES" ] && return 0
    local toml v=""
    toml="$(rp /etc/cachy-void/updater.toml)"
    if [ -f "$toml" ]; then
        v="$(grep -E '^[[:space:]]*void_packages[[:space:]]*=' "$toml" \
             | head -n1 | sed -E 's/^[^=]*=[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/')"
        [ -n "$v" ] && { VOID_PACKAGES="$v"; return 0; }
    fi
    if [ -z "$ROOT" ] && [ -n "${SUDO_USER:-}" ]; then
        local home
        home="$(getent passwd "$SUDO_USER" | cut -d: -f6)"
        [ -n "$home" ] && [ -d "$home/void-packages" ] && VOID_PACKAGES="$home/void-packages"
    fi
}

# toml_get SECTION KEY — read a scalar string from an updater.toml section.
# Crude (no nested tables), but the updater.toml the user hand-writes (§6.1) is
# flat; matches resolve_void_packages' spirit. Prints nothing / returns 1 if the
# file or key is absent, so callers can fall back to their default.
toml_get() {
    local section="$1" key="$2" toml
    toml="$(rp /etc/cachy-void/updater.toml)"
    [ -f "$toml" ] || return 1
    # Portable awk (no gawk-only gensub — Void's default awk is mawk): literal
    # section compare + index/substr key split, so it runs anywhere deploy.sh does.
    awk -v sec="[$section]" -v key="$key" '
        { t=$0; sub(/^[[:space:]]+/,"",t); sub(/[[:space:]]+$/,"",t) }
        t ~ /^\[/ { in_sec = (t == sec); next }
        in_sec {
            line=$0; sub(/#.*/,"",line)
            n=index(line,"=")
            if (n>0) {
                k=substr(line,1,n-1); sub(/^[[:space:]]+/,"",k); sub(/[[:space:]]+$/,"",k)
                if (k==key) {
                    v=substr(line,n+1); sub(/^[[:space:]]+/,"",v); sub(/[[:space:]]+$/,"",v)
                    gsub(/^["'"'"']|["'"'"']$/,"",v)   # strip surrounding quotes
                    print v; exit
                }
            }
        }' "$toml"
}
resolve_snapshot_dir()    { local v; v="$(toml_get snapshot dir    2>/dev/null)"; printf '%s' "${v:-$SNAP_DIR_DEFAULT}"; }
resolve_snapshot_subvol() { local v; v="$(toml_get snapshot subvol 2>/dev/null)"; printf '%s' "${v:-/}"; }
resolve_snapshot_enable() { local v; v="$(toml_get snapshot enable 2>/dev/null)"; printf '%s' "${v:-auto}"; }

# install_snapshot_subvol — create the §9.5 pre-deploy snapshot subvol.
# The updater takes a read-only snapshot INTO this subvol before every deploy and
# ASSUMES it already exists (engine/snapshot.py; the updater's sudo grants cover
# btrfs snapshot/delete/list but NOT create — so creation is deploy.sh's job, as
# root). Created once, only when the target subvol is btrfs; harmless if snapshots
# end up disabled. If the root is converted to btrfs LATER (a real path — the
# Medion did exactly this), just re-run deploy.sh and this step creates it.
install_snapshot_subvol() {
    local enable snapdir subvol fstype pdir
    enable="$(resolve_snapshot_enable)"
    if [ "$enable" = false ] || [ "$enable" = "\"false\"" ]; then
        log "pre-deploy snapshots disabled in updater.toml — skipping subvol (§9.5)"
        return 0
    fi
    snapdir="$(resolve_snapshot_dir)"
    subvol="$(resolve_snapshot_subvol)"
    if [ -n "$ROOT" ]; then
        warn "offline mode: §9.5 snapshot subvol '$snapdir' not created — after"
        warn "  booting Void run: sudo btrfs subvolume create $snapdir (if on btrfs)"
        return 0
    fi
    if $SIMULATE; then
        log "[simulate] would create btrfs subvol $snapdir when '$subvol' is btrfs (§9.5)"
        return 0
    fi
    fstype="$(findmnt -no FSTYPE -T "$(rp "$subvol")" 2>/dev/null || true)"
    if [ "$fstype" != "btrfs" ]; then
        log "'$subvol' is ${fstype:-unknown} (not btrfs) — skipping §9.5 snapshot subvol (auto-mode disables snapshots there)"
        return 0
    fi
    pdir="$(rp "$snapdir")"
    if [ -e "$pdir" ]; then
        log "§9.5 snapshot subvol $snapdir already present"
        manifest_add SUBVOL "$snapdir" "-"
        return 0
    fi
    if $DRY_RUN; then log "[dry-run] btrfs subvolume create $snapdir"; return 0; fi
    if btrfs subvolume create "$pdir" >/dev/null; then
        manifest_add SUBVOL "$snapdir" "-"
        ok "created btrfs subvol $snapdir (§9.5 pre-deploy snapshot target)"
    else
        warn "could not create btrfs subvol $snapdir — §9.5 snapshots will fail until"
        warn "  it exists; create it manually: sudo btrfs subvolume create $snapdir"
    fi
}

# ---------------------------------------------------------------------------
# Install flow
# ---------------------------------------------------------------------------
install_sudoers() {
    # Rendered and validated before it is ever placed in /etc/sudoers.d.
    local tmp dest="/etc/sudoers.d/cachy-void"
    tmp="$(mktemp)"; render "$SYS_DIR/sudoers.d/cachy-void" "$tmp"
    if ! visudo -cqf "$tmp"; then
        rm -f -- "$tmp"
        die "generated sudoers fragment failed validation — refusing to install"
    fi
    install_file "$tmp" "$dest" 0440 root root
    rm -f -- "$tmp"
}

install_compiler_profile() {
    if [ -z "$VOID_PACKAGES" ]; then
        warn "void-packages path unknown — skipping etc/conf and xbps.d overlay."
        warn "  re-run with --void-packages DIR (or set [paths] void_packages in"
        warn "  /etc/cachy-void/updater.toml) to install them."
        PARTIAL=true
        return 0
    fi
    local pvp; pvp="$(rp "$VOID_PACKAGES")"
    [ -d "$pvp" ] || die "void-packages dir does not exist: $pvp"
    local vp_owner vp_group tmp
    vp_owner="$(stat -c '%U' "$pvp")"
    vp_group="$(stat -c '%G' "$pvp")"

    # §1.1 compiler profile → <void-packages>/etc/conf, owned by the build user.
    tmp="$(mktemp)"; render "$SYS_DIR/etc/conf" "$tmp"
    install_file "$tmp" "$VOID_PACKAGES/etc/conf" 0644 "$vp_owner" "$vp_group"
    rm -f -- "$tmp"

    # §4.6/§7.2 overlay repository registration → /etc/xbps.d.
    tmp="$(mktemp)"; render "$SYS_DIR/xbps.d/00-cachy-overlay.conf" "$tmp"
    install_file "$tmp" "/etc/xbps.d/00-cachy-overlay.conf" 0644 root root
    rm -f -- "$tmp"
}

# --with-grub performs BOTH sanctioned bootloader edits (§3.3, §8.6):
#   * usbcore.autosuspend=-1 on the kernel cmdline (input latency)
#   * GRUB_DEFAULT=saved — prerequisite for one-shot kernel staging; without
#     it grub-set-default writes are silently ignored and the updater refuses
#     staging as "manual-unsafe" (§8.6). This edit belongs HERE (root, backed
#     up, ledger-tracked, reversible), never in the updater process.
install_grub_settings() {
    $WITH_GRUB || { log "GRUB edits skipped (pass --with-grub for §3.3 autosuspend + §8.6 GRUB_DEFAULT=saved)"; return 0; }
    local grubcfg="/etc/default/grub" param="usbcore.autosuspend=-1"
    local pgrub; pgrub="$(rp "$grubcfg")"
    [ -f "$pgrub" ] || { warn "$grubcfg not found — skipping GRUB edits"; return 0; }

    local need_param=true need_saved=true
    grep -q "$param" "$pgrub" && need_param=false
    grep -q '^GRUB_DEFAULT=saved$' "$pgrub" && need_saved=false
    if ! $need_param && ! $need_saved; then
        log "GRUB already configured (autosuspend + GRUB_DEFAULT=saved)"
        return 0
    fi
    if $DRY_RUN; then
        $need_param && log "[dry-run] add $param to GRUB_CMDLINE_LINUX_DEFAULT"
        $need_saved && log "[dry-run] set GRUB_DEFAULT=saved"
        log "[dry-run] regenerate grub config"
        return 0
    fi

    if ! manifest_has GRUB "$grubcfg"; then
        local backup="${grubcfg}.pre-cachy.${TS}.bak"
        cp -a -- "$pgrub" "$(rp "$backup")"
        manifest_add GRUB "$grubcfg" "$backup"
        warn "backed up $grubcfg -> $backup"
    fi
    if $need_param; then
        if grep -q '^GRUB_CMDLINE_LINUX_DEFAULT=' "$pgrub"; then
            sed -i -E "s|^(GRUB_CMDLINE_LINUX_DEFAULT=\"[^\"]*)\"|\1 ${param}\"|" "$pgrub"
        else
            printf 'GRUB_CMDLINE_LINUX_DEFAULT="%s"\n' "$param" >> "$pgrub"
        fi
        ok "kernel cmdline: added $param"
    fi
    if $need_saved; then
        if grep -q '^GRUB_DEFAULT=' "$pgrub"; then
            sed -i -E 's|^GRUB_DEFAULT=.*|GRUB_DEFAULT=saved|' "$pgrub"
        else
            printf 'GRUB_DEFAULT=saved\n' >> "$pgrub"
        fi
        ok "GRUB_DEFAULT=saved (§8.6 one-shot staging prerequisite)"
    fi
    grub_regen
}

install_kernel_state() {
    # §8.1: kernel/ state dir owned by the build user so the unprivileged
    # updater records staging transitions without widening sudo.
    install_dir "$STATE_DIR/kernel" "$CHOWN_USER"
    ok "kernel state dir ready ($STATE_DIR/kernel, owner $CHOWN_USER)"
    # §8.5: runtime copy of the kernel config fragment for the G2 gate.
    install_file "$SRC_DIR/overlay/config/cachy-fragment.config" \
                 /etc/cachy-void/cachy-fragment.config 0644 root root
}

do_install() {
    [ -d "$SYS_DIR" ] || die "cannot find system/ next to deploy.sh (looked in $SYS_DIR)"
    [ -n "$UPDATER_USER" ] || die "cannot determine updater user — pass --user NAME"
    CHOWN_USER="$UPDATER_USER"
    if ! id "$UPDATER_USER" >/dev/null 2>&1; then
        if [ -n "$ROOT" ]; then
            warn "user $UPDATER_USER not present on this rescue OS — user-owned"
            warn "  dirs fall back to root; chown them after booting Void"
            CHOWN_USER="root"
        else
            die "user does not exist: $UPDATER_USER"
        fi
    fi
    [ -n "$MAKEJOBS" ] || MAKEJOBS="$(nproc)"
    if [ -z "$MARCH" ]; then
        MARCH="$(detect_march)"
        if [ -n "$ROOT" ]; then
            warn "auto-detected -march=$MARCH from THIS host's CPU — pass --march"
            warn "  explicitly if the target machine's CPU differs from this one"
        else
            log "auto-detected -march=$MARCH (override with --march)"
        fi
    fi
    resolve_void_packages
    PARTIAL=false

    detect_sandbox

    log "Cachy-Void install — user=$UPDATER_USER march=$MARCH jobs=$MAKEJOBS tag=$DEPLOY_TAG"
    log "void-packages: ${VOID_PACKAGES:-<unresolved>}   engine: $CACHY_ENGINE"
    [ -n "$ROOT" ] && warn "offline mode: writing into $ROOT (packages + runtime steps skipped)"
    $DRY_RUN && warn "dry-run: no changes will be made"
    $SIMULATE && [ -z "$ROOT" ] && warn "simulate: runit service enablement will be skipped"

    log "[1/10] sysctl, udev, modprobe, module-load profiles (§3.1, §3.3)"
    install_file "$SYS_DIR/sysctl.d/99-cachy-gaming.conf"   /etc/sysctl.d/99-cachy-gaming.conf   0644 root root
    install_file "$SYS_DIR/udev/60-ioschedulers.rules"      /etc/udev/rules.d/60-ioschedulers.rules 0644 root root
    install_file "$SYS_DIR/modprobe.d/99-gaming-input.conf" /etc/modprobe.d/99-gaming-input.conf 0644 root root
    install_file "$SYS_DIR/modules-load.d/cachy.conf"       /etc/modules-load.d/cachy.conf       0644 root root

    log "[2/10] updater privilege boundary (§4.1)"
    install_sudoers

    log "[3/10] kernel state dir + G2 config fragment (§8.1, §8.5)"
    install_kernel_state

    log "[4/10] compiler profile + overlay repository (§1.1, §4.6, §7.2)"
    install_compiler_profile

    log "[5/10] mirror the updater engine into $CACHY_ENGINE (§6/§8.9)"
    install_engine

    log "[6/10] packages: zram (§3.2) + xtools (§4.7 cycling) + snooze (§4.9 timer)"
    ensure_pkg "$PKG_ZRAM"
    ensure_pkg "$PKG_XTOOLS"
    # snooze backs the §4.9 service; install it unconditionally so the service
    # works the moment it is enabled — whether via --with-schedule or a later
    # manual `ln -s` (the run script documents that path). It is tiny.
    ensure_pkg "$PKG_SNOOZE"

    log "[7/10] runit services: zram (§3.2), cachy-health (§8.7), cachy-void-update (§4.9)"
    install_file "$SYS_DIR/sv/zramen/conf" /etc/sv/zramen/conf 0644 root root
    enable_service "$PKG_ZRAM"
    install_health_service
    install_schedule_service

    log "[8/10] gaming userspace layer: gamemode + MangoHud + cachy-game (§3.4)"
    install_gaming_userspace

    if $WITH_BRANDING; then
        log "[+] void-tactical desktop branding (opt-in, --with-branding)"
        install_branding
    fi

    log "[9/10] pre-deploy snapshot subvol (§9.5, btrfs hosts only)"
    install_snapshot_subvol

    log "[10/10] apply runtime state"
    install_grub_settings
    if ! $DRY_RUN && live; then
        # bbr module must be present BEFORE sysctl applies tcp_congestion_control
        modprobe tcp_bbr 2>/dev/null || warn "could not load tcp_bbr now (built into linux-cachy; harmless on stock kernel until reboot)"
        sysctl --system >/dev/null 2>&1 || warn "sysctl --system reported errors (some keys may be unsupported on the running kernel)"
        udevadm control --reload && udevadm trigger || warn "udev reload failed"
    fi

    ok "install complete (ledger tag: $DEPLOY_TAG)."
    $PARTIAL && warn "PARTIAL: compiler profile/overlay were skipped (see above)."
    cat <<EOF

Next steps / notes:
  * Reboot for kernel-level settings (mousepoll, module loads) to take full effect.
  * The health daemon runs as: chpst -u $UPDATER_USER $CACHY_ENGINE/cachy_void_update.py --health-daemon
  * zram conf uses the verified zramen-1.0.1 names (ZRAM_COMP_ALGORITHM,
    ZRAM_SIZE=percent, ZRAM_MAX_SIZE, ZRAM_PRIORITY) — see /etc/sv/zramen/conf.
  * -march was '$MARCH' (auto-detected via the §1.2 ladder unless --march was given).
  * USB autosuspend (§3.3) is opt-in: re-run with --with-grub to apply it.
  * Unattended updates (§4.9) are opt-in: re-run with --with-schedule to enable the
    daily cachy-void-update timer (edit /etc/sv/cachy-void-update/conf for the time).
  * Pre-deploy snapshots (§9.5) auto-arm on btrfs. If you convert to btrfs LATER,
    re-run deploy.sh once so it creates the $SNAP_DIR_DEFAULT subvol.
  * Desktop branding (void-tactical LXQt) is opt-in: re-run with --with-branding to
    install the toolkit, then apply the look as your user:  cachy-branding
    (revert with: cachy-branding --remove).
  * Inspect the change ledger any time:  sudo $0 --log
  * Roll back everything:                sudo $0 --uninstall
  * Roll back one route's changes:       sudo $0 --uninstall-tag $DEPLOY_TAG
  * Roll back a single item:             sudo $0 --uninstall-item /etc/sysctl.d/99-cachy-gaming.conf
EOF
}

# ---------------------------------------------------------------------------
# Uninstall flow — replay ledger entries in reverse (all, one tag, or one item)
# ---------------------------------------------------------------------------
uninstall_file() {  # target backup   (logical paths)
    local target="$1" backup="$2" ptarget pbackup
    ptarget="$(rp "$target")"
    if $DRY_RUN; then log "[dry-run] remove $target${backup:+ (restore $backup)}"; return; fi
    rm -f -- "$ptarget"
    if [ "$backup" != "-" ] && [ -e "$(rp "$backup")" ]; then
        pbackup="$(rp "$backup")"
        mv -- "$pbackup" "$ptarget"
        ok "restored $target from backup"
    else
        ok "removed $target"
    fi
}
uninstall_service() {  # name
    local name="$1"
    if $DRY_RUN; then log "[dry-run] disable service $name"; return; fi
    if live; then
        sv down "$name" >/dev/null 2>&1 || true
    fi
    # live enablement + offline enablement locations; -f tolerates absence
    rm -f -- "$(rp "/var/service/$name")" "$(rp "/etc/runit/runsvdir/default/$name")"
    ok "disabled runit service $name"
}
uninstall_pkg() {  # pkg
    local pkg="$1"
    if [ -n "$ROOT" ]; then
        warn "offline mode: package '$pkg' left installed — remove it from a chroot"
        return 0
    fi
    if $DRY_RUN; then log "[dry-run] xbps-remove $pkg"; return; fi
    if xbps-query -- "$pkg" >/dev/null 2>&1; then
        xbps-remove -y -- "$pkg" && ok "removed package $pkg" \
            || warn "could not remove $pkg (still in use?) — left installed"
    fi
}
uninstall_dir() {  # dir we created; may hold runtime state (kernel-state.json)
    local d="$1"
    if $DRY_RUN; then log "[dry-run] remove dir $d"; return; fi
    rm -rf -- "$(rp "$d")"
    ok "removed dir $d"
}
uninstall_subvol() {  # subvol path (logical) — NEVER nuke rollback nets silently
    local d="$1" pd; pd="$(rp "$d")"
    if $DRY_RUN; then log "[dry-run] delete btrfs subvol $d (only if it holds no snapshots)"; return; fi
    [ -e "$pd" ] || { ok "subvol $d already gone"; return; }
    if ls -1d "$pd"/deploy-* >/dev/null 2>&1; then
        warn "subvol $d still holds pre-deploy snapshot(s) — LEFT IN PLACE"
        warn "  (a §9.5 rollback net; remove manually when sure:"
        warn "   sudo btrfs subvolume delete $d/deploy-* && sudo btrfs subvolume delete $d)"
        return
    fi
    if live && command -v btrfs >/dev/null 2>&1; then
        btrfs subvolume delete "$pd" >/dev/null 2>&1 && ok "deleted empty subvol $d" \
            || { rmdir "$pd" 2>/dev/null && ok "removed $d" || warn "could not remove subvol $d"; }
    else
        warn "offline/no btrfs here: leaving subvol $d in place (delete from booted Void)"
    fi
}
uninstall_grub() {  # target backup
    local target="$1" backup="$2"
    if $DRY_RUN; then log "[dry-run] restore $target and regenerate grub"; return; fi
    if [ "$backup" != "-" ] && [ -e "$(rp "$backup")" ]; then
        mv -- "$(rp "$backup")" "$(rp "$target")" && ok "restored $target"
    fi
    grub_regen
}

do_uninstall() {
    [ -f "$MANIFEST" ] || die "no ledger at $MANIFEST — nothing to uninstall"
    local scope="everything"
    [ -n "$UN_TAG" ]  && scope="entries tagged '$UN_TAG'"
    [ -n "$UN_ITEM" ] && scope="item '$UN_ITEM'"
    log "Cachy-Void rollback — replaying $scope from $MANIFEST in reverse"
    $DRY_RUN && warn "dry-run: no changes will be made"

    # Partition the ledger into matching (to replay) and kept (to preserve).
    local -a all=() matching=() kept=()
    mapfile -t all < "$MANIFEST"
    local line type target extra tag ts
    for line in "${all[@]}"; do
        [ -n "$line" ] || continue
        IFS="$TAB" read -r type target extra tag ts <<< "$line"
        tag="${tag:-core}"
        if { [ -n "$UN_TAG" ] && [ "$tag" != "$UN_TAG" ]; } ||
           { [ -n "$UN_ITEM" ] && [ "$target" != "$UN_ITEM" ]; }; then
            kept+=("$line")
        else
            matching+=("$line")
        fi
    done
    if [ "${#matching[@]}" -eq 0 ]; then
        log "no ledger entries match — nothing to do."
        return 0
    fi

    local i
    for ((i=${#matching[@]}-1; i>=0; i--)); do
        IFS="$TAB" read -r type target extra tag ts <<< "${matching[$i]}"
        case "$type" in
            SERVICE) uninstall_service "$target" ;;
            FILE)    uninstall_file    "$target" "$extra" ;;
            DIR)     uninstall_dir     "$target" ;;
            SUBVOL)  uninstall_subvol  "$target" ;;
            GRUB)    uninstall_grub    "$target" "$extra" ;;
            PKG)     uninstall_pkg     "$target" ;;
            *)       warn "unknown ledger entry: $type $target" ;;
        esac
    done

    if ! $DRY_RUN; then
        if [ "${#kept[@]}" -gt 0 ]; then
            printf '%s\n' "${kept[@]}" > "$MANIFEST"
            log "ledger updated: ${#matching[@]} entr(y/ies) rolled back, ${#kept[@]} kept"
        else
            rm -f -- "$MANIFEST"
            rmdir --ignore-fail-on-non-empty -- "$(rp "$STATE_DIR")" 2>/dev/null || true
        fi
        if live; then
            sysctl --system >/dev/null 2>&1 || true
            udevadm control --reload && udevadm trigger 2>/dev/null || true
        fi
    fi
    ok "rollback complete ($scope)."
}

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
main() {
    parse_args "$@"
    require_root
    MANIFEST="$(rp "$STATE_DIR")/deploy.manifest"
    if $DO_LOG; then
        do_log
    elif $DO_UNINSTALL; then
        do_uninstall
    else
        do_install
    fi
}
main "$@"
