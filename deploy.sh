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
#                    [--jobs N] [--with-grub] [--tag core|test|opt]
#                    [--simulate] [--dry-run] [--root DIR]
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

# Packages this deliverable installs (§3.2 zram). Game controllers need no
# package on Void — the kernel + elogind seat uaccess handle standard pads, and
# Steam ships its own rules (§3.3); there is no game-devices-udev package.
readonly PKG_ZRAM="zramen"

# Fixed install location for the mirrored Python engine (§6/§8.9).
readonly CACHY_ENGINE="/usr/libexec/cachy-void-updater"
readonly HEALTH_LOG_DIR="/var/log/cachy-health"

# ---------------------------------------------------------------------------
# Options (mutable)
# ---------------------------------------------------------------------------
DO_UNINSTALL=false
DO_LOG=false
DRY_RUN=false
WITH_GRUB=false
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

# ---------------------------------------------------------------------------
# Ledger — the single source of truth for what we changed on the target.
# Format:  TYPE<TAB>TARGET<TAB>EXTRA<TAB>TAG<TAB>TS   (v1 lines had 3 fields;
# they parse with tag defaulting to "core"). All paths are LOGICAL in-Void
# paths, so the same ledger drives live and --root rollbacks identically.
#   FILE     dest-path          backup-path or "-"
#   SERVICE  service-name       "-"
#   PKG      package-name       "-"        (only recorded if WE installed it)
#   DIR      dir-path           "-"
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

    log "[1/8] sysctl, udev, modprobe, module-load profiles (§3.1, §3.3)"
    install_file "$SYS_DIR/sysctl.d/99-cachy-gaming.conf"   /etc/sysctl.d/99-cachy-gaming.conf   0644 root root
    install_file "$SYS_DIR/udev/60-ioschedulers.rules"      /etc/udev/rules.d/60-ioschedulers.rules 0644 root root
    install_file "$SYS_DIR/modprobe.d/99-gaming-input.conf" /etc/modprobe.d/99-gaming-input.conf 0644 root root
    install_file "$SYS_DIR/modules-load.d/cachy.conf"       /etc/modules-load.d/cachy.conf       0644 root root

    log "[2/8] updater privilege boundary (§4.1)"
    install_sudoers

    log "[3/8] kernel state dir + G2 config fragment (§8.1, §8.5)"
    install_kernel_state

    log "[4/8] compiler profile + overlay repository (§1.1, §4.6, §7.2)"
    install_compiler_profile

    log "[5/8] mirror the updater engine into $CACHY_ENGINE (§6/§8.9)"
    install_engine

    log "[6/8] package: zram (§3.2)"
    ensure_pkg "$PKG_ZRAM"

    log "[7/8] runit services: zram (§3.2) + cachy-health (§8.7)"
    install_file "$SYS_DIR/sv/zramen/conf" /etc/sv/zramen/conf 0644 root root
    enable_service "$PKG_ZRAM"
    install_health_service

    log "[8/8] apply runtime state"
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
