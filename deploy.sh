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
#   * Reversible  — every action is recorded in a manifest; --uninstall replays
#                   it in reverse, restoring backed-up originals and removing
#                   only the packages/dirs this script installed.
#   * Fail-safe   — the sudoers fragment is validated with `visudo -c` before it
#                   is ever activated; a bad rule can never lock out sudo.
#   * Sandbox-safe — WSL2/virtualized profiles auto-enable --simulate: files are
#                   laid down but runit service enablement is skipped.
#
# Usage:
#   sudo ./deploy.sh [--user NAME] [--void-packages DIR] [--march ARCH]
#                    [--jobs N] [--with-grub] [--simulate] [--dry-run]
#   sudo ./deploy.sh --uninstall [--dry-run]
#
# Run `./deploy.sh --help` for the full option list.

set -euo pipefail

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
readonly SRC_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SYS_DIR="$SRC_DIR/system"
readonly STATE_DIR="/var/lib/cachy-void"
readonly MANIFEST="$STATE_DIR/deploy.manifest"
readonly TS="$(date +%Y%m%d%H%M%S)"
readonly TAB=$'\t'

# Packages this deliverable is responsible for (§3.2 zram, §3.3 controllers).
readonly PKG_ZRAM="zramen"
readonly PKG_CONTROLLERS="game-devices-udev"

# Fixed install location for the mirrored Python engine (§6/§8.9).
readonly CACHY_ENGINE="/usr/libexec/cachy-void-updater"
readonly HEALTH_LOG_DIR="/var/log/cachy-health"

# ---------------------------------------------------------------------------
# Options (mutable)
# ---------------------------------------------------------------------------
DO_UNINSTALL=false
DRY_RUN=false
WITH_GRUB=false
SIMULATE=false            # WSL2/sandbox: lay down files, skip init-dependent ops
UPDATER_USER="${SUDO_USER:-}"
VOID_PACKAGES="${VOID_PACKAGES:-}"
MARCH="x86-64-v3"
MAKEJOBS=""

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
    sed -n '3,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --uninstall)      DO_UNINSTALL=true ;;
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
}

require_root() {
    [ "$(id -u)" -eq 0 ] || die "must run as root — try: sudo $0 $*"
}

# Detect a virtualized/sandbox profile with no runit PID 1 (WSL2). In that case
# we still install files but skip init-dependent operations (service enablement),
# so provisioning is exercised without destructive/pointless supervisor changes.
detect_sandbox() {
    $SIMULATE && { log "simulate mode: skipping init-dependent operations"; return; }
    if grep -qiE 'microsoft|wsl' /proc/version 2>/dev/null; then
        SIMULATE=true
        warn "WSL2/virtualized profile detected — enabling --simulate (no runit alterations)"
    fi
}

# ---------------------------------------------------------------------------
# Manifest helpers — the single source of truth for what we changed
# Format:  TYPE<TAB>TARGET<TAB>EXTRA
#   FILE     dest-path          backup-path or "-"
#   SERVICE  service-name       "-"
#   PKG      package-name       "-"        (only recorded if WE installed it)
#   GRUB     /etc/default/grub  backup-path
# ---------------------------------------------------------------------------
manifest_has() {  # TYPE TARGET
    [ -f "$MANIFEST" ] && grep -qF -- "$1$TAB$2$TAB" "$MANIFEST"
}
manifest_add() {  # TYPE TARGET EXTRA
    manifest_has "$1" "$2" && return 0
    mkdir -p -- "$STATE_DIR"
    printf '%s%s%s%s%s\n' "$1" "$TAB" "$2" "$TAB" "$3" >> "$MANIFEST"
}

# ---------------------------------------------------------------------------
# Install primitives
# ---------------------------------------------------------------------------
# install_file SRC DEST MODE OWNER GROUP
install_file() {
    local src="$1" dest="$2" mode="$3" owner="$4" group="$5" backup="-"
    if $DRY_RUN; then log "[dry-run] install $dest  ($mode $owner:$group)"; return; fi
    mkdir -p -- "$(dirname -- "$dest")"
    if [ -e "$dest" ] || [ -L "$dest" ]; then
        if manifest_has FILE "$dest"; then
            :  # already managed by us — overwrite in place, keep original backup
        else
            backup="${dest}.pre-cachy.${TS}.bak"
            cp -a -- "$dest" "$backup"
            warn "backed up existing $dest -> $backup"
        fi
    fi
    install -m "$mode" -o "$owner" -g "$group" -- "$src" "$dest"
    manifest_add FILE "$dest" "$backup"
    ok "installed $dest"
}

# render SRC DEST_TMP — substitute host-specific tokens into a config template
render() {
    sed -e "s|@USER@|${UPDATER_USER}|g" \
        -e "s|@MARCH@|${MARCH}|g" \
        -e "s|@MAKEJOBS@|${MAKEJOBS}|g" \
        -e "s|@VOID_PACKAGES@|${VOID_PACKAGES}|g" \
        -e "s|@ENGINE@|${CACHY_ENGINE}|g" \
        -- "$1" > "$2"
}

# ensure_pkg PKG [optional] — install only if absent; record only what we added.
# An "optional" package that is absent from the repositories is skipped with a
# warning instead of aborting the install.
ensure_pkg() {
    local pkg="$1" optional="${2:-}"
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

# enable_service NAME — runit: symlink /etc/sv/NAME into /var/service (§3.2)
enable_service() {
    local name="$1" link="/var/service/$1" svdir="/etc/sv/$1"
    if $DRY_RUN; then log "[dry-run] enable runit service $name"; return; fi
    if $SIMULATE; then
        log "[simulate] would enable runit service $name (no PID 1 runit here)"
        return
    fi
    [ -d "$svdir" ] || die "service dir $svdir missing (is $name installed?)"
    if [ -L "$link" ]; then
        log "service $name already enabled"
    else
        ln -s -- "$svdir" "$link"
        ok "enabled runit service $name"
    fi
    manifest_add SERVICE "$name" "-"
}

# install_dir DIR OWNER — create a tracked directory (removed on uninstall).
install_dir() {
    local dir="$1" owner="${2:-root}"
    if $DRY_RUN; then log "[dry-run] install -d -o $owner $dir"; return; fi
    install -d -o "$owner" -- "$dir"
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
    local toml="/etc/cachy-void/updater.toml" v=""
    if [ -f "$toml" ]; then
        v="$(grep -E '^[[:space:]]*void_packages[[:space:]]*=' "$toml" \
             | head -n1 | sed -E 's/^[^=]*=[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/')"
        [ -n "$v" ] && { VOID_PACKAGES="$v"; return 0; }
    fi
    if [ -n "${SUDO_USER:-}" ]; then
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
    [ -d "$VOID_PACKAGES" ] || die "void-packages dir does not exist: $VOID_PACKAGES"
    local vp_owner vp_group tmp
    vp_owner="$(stat -c '%U' "$VOID_PACKAGES")"
    vp_group="$(stat -c '%G' "$VOID_PACKAGES")"

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
#     up, manifest-tracked, reversible), never in the updater process.
install_grub_settings() {
    $WITH_GRUB || { log "GRUB edits skipped (pass --with-grub for §3.3 autosuspend + §8.6 GRUB_DEFAULT=saved)"; return 0; }
    local grubcfg="/etc/default/grub" param="usbcore.autosuspend=-1"
    [ -f "$grubcfg" ] || { warn "$grubcfg not found — skipping GRUB edits"; return 0; }

    local need_param=true need_saved=true
    grep -q "$param" "$grubcfg" && need_param=false
    grep -q '^GRUB_DEFAULT=saved$' "$grubcfg" && need_saved=false
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
        cp -a -- "$grubcfg" "$backup"
        manifest_add GRUB "$grubcfg" "$backup"
        warn "backed up $grubcfg -> $backup"
    fi
    if $need_param; then
        if grep -q '^GRUB_CMDLINE_LINUX_DEFAULT=' "$grubcfg"; then
            sed -i -E "s|^(GRUB_CMDLINE_LINUX_DEFAULT=\"[^\"]*)\"|\1 ${param}\"|" "$grubcfg"
        else
            printf 'GRUB_CMDLINE_LINUX_DEFAULT="%s"\n' "$param" >> "$grubcfg"
        fi
        ok "kernel cmdline: added $param"
    fi
    if $need_saved; then
        if grep -q '^GRUB_DEFAULT=' "$grubcfg"; then
            sed -i -E 's|^GRUB_DEFAULT=.*|GRUB_DEFAULT=saved|' "$grubcfg"
        else
            printf 'GRUB_DEFAULT=saved\n' >> "$grubcfg"
        fi
        ok "GRUB_DEFAULT=saved (§8.6 one-shot staging prerequisite)"
    fi
    grub_regen
}

install_kernel_state() {
    # §8.1: kernel/ state dir owned by the build user so the unprivileged
    # updater records staging transitions without widening sudo.
    local kdir="$STATE_DIR/kernel"
    if $DRY_RUN; then
        log "[dry-run] install -d -o $UPDATER_USER $kdir"
    else
        install -d -o "$UPDATER_USER" -- "$kdir"
        manifest_add DIR "$kdir" "-"
        ok "created $kdir (owner $UPDATER_USER)"
    fi
    # §8.5: runtime copy of the kernel config fragment for the G2 gate.
    install_file "$SRC_DIR/overlay/config/cachy-fragment.config" \
                 /etc/cachy-void/cachy-fragment.config 0644 root root
}

do_install() {
    [ -d "$SYS_DIR" ] || die "cannot find system/ next to deploy.sh (looked in $SYS_DIR)"
    [ -n "$UPDATER_USER" ] || die "cannot determine updater user — pass --user NAME"
    id "$UPDATER_USER" >/dev/null 2>&1 || die "user does not exist: $UPDATER_USER"
    [ -n "$MAKEJOBS" ] || MAKEJOBS="$(nproc)"
    resolve_void_packages
    PARTIAL=false

    detect_sandbox

    log "Cachy-Void install — user=$UPDATER_USER march=$MARCH jobs=$MAKEJOBS"
    log "void-packages: ${VOID_PACKAGES:-<unresolved>}   engine: $CACHY_ENGINE"
    $DRY_RUN && warn "dry-run: no changes will be made"
    $SIMULATE && warn "simulate: runit service enablement will be skipped"

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

    log "[6/8] packages (§3.2 zram, §3.3 controllers)"
    ensure_pkg "$PKG_ZRAM"
    ensure_pkg "$PKG_CONTROLLERS" optional

    log "[7/8] runit services: zram (§3.2) + cachy-health (§8.7)"
    install_file "$SYS_DIR/sv/zramen/conf" /etc/sv/zramen/conf 0644 root root
    enable_service "$PKG_ZRAM"
    install_health_service

    log "[8/8] apply runtime state"
    install_grub_settings
    if ! $DRY_RUN && ! $SIMULATE; then
        sysctl --system >/dev/null 2>&1 || warn "sysctl --system reported errors (some keys may be unsupported on the running kernel)"
        udevadm control --reload && udevadm trigger || warn "udev reload failed"
        modprobe tcp_bbr 2>/dev/null || warn "could not load tcp_bbr now (built into linux-cachy; harmless on stock kernel until reboot)"
    fi

    ok "install complete."
    $PARTIAL && warn "PARTIAL: compiler profile/overlay were skipped (see above)."
    cat <<EOF

Next steps / notes:
  * Reboot for kernel-level settings (mousepoll, module loads) to take full effect.
  * The health daemon runs as: chpst -u $UPDATER_USER $CACHY_ENGINE/cachy_void_update.py --health-daemon
  * Verify zramen variable names against the installed package (§3.2 caveat):
      cat /etc/sv/zramen/run   # confirm ZRAM_ALG / ZRAM_SIZE / ZRAM_PRIO
  * For x86-64-v4, re-run with: --march x86-64-v4  (only on Zen 4/5 — see §1.2).
  * USB autosuspend (§3.3) is opt-in: re-run with --with-grub to apply it.
  * Revert everything with: sudo $0 --uninstall
EOF
}

# ---------------------------------------------------------------------------
# Uninstall flow — replay the manifest in reverse
# ---------------------------------------------------------------------------
uninstall_file() {  # target backup
    local target="$1" backup="$2"
    if $DRY_RUN; then log "[dry-run] remove $target${backup:+ (restore $backup)}"; return; fi
    rm -f -- "$target"
    if [ "$backup" != "-" ] && [ -e "$backup" ]; then
        mv -- "$backup" "$target"
        ok "restored $target from backup"
    else
        ok "removed $target"
    fi
}
uninstall_service() {  # name
    local name="$1"
    if $DRY_RUN; then log "[dry-run] disable service $name"; return; fi
    [ -L "/var/service/$name" ] || { log "service $name already disabled"; return; }
    sv down "$name" >/dev/null 2>&1 || true
    rm -f -- "/var/service/$name"
    ok "disabled runit service $name"
}
uninstall_pkg() {  # pkg
    local pkg="$1"
    if $DRY_RUN; then log "[dry-run] xbps-remove $pkg"; return; fi
    if xbps-query -- "$pkg" >/dev/null 2>&1; then
        xbps-remove -y -- "$pkg" && ok "removed package $pkg" \
            || warn "could not remove $pkg (still in use?) — left installed"
    fi
}
uninstall_dir() {  # dir we created; may hold runtime state (kernel-state.json)
    local d="$1"
    if $DRY_RUN; then log "[dry-run] remove dir $d"; return; fi
    rm -rf -- "$d"
    ok "removed dir $d"
}
uninstall_grub() {  # target backup
    local target="$1" backup="$2"
    if $DRY_RUN; then log "[dry-run] restore $target and regenerate grub"; return; fi
    [ "$backup" != "-" ] && [ -e "$backup" ] && mv -- "$backup" "$target" && ok "restored $target"
    grub_regen
}

do_uninstall() {
    [ -f "$MANIFEST" ] || die "no manifest at $MANIFEST — nothing to uninstall"
    log "Cachy-Void uninstall — replaying $MANIFEST in reverse"
    $DRY_RUN && warn "dry-run: no changes will be made"

    local -a lines=()
    mapfile -t lines < <(tac "$MANIFEST")
    local type target extra
    for line in "${lines[@]}"; do
        [ -n "$line" ] || continue
        IFS="$TAB" read -r type target extra <<< "$line"
        case "$type" in
            SERVICE) uninstall_service "$target" ;;
            FILE)    uninstall_file    "$target" "$extra" ;;
            DIR)     uninstall_dir     "$target" ;;
            GRUB)    uninstall_grub    "$target" "$extra" ;;
            PKG)     uninstall_pkg     "$target" ;;
            *)       warn "unknown manifest entry: $type $target" ;;
        esac
    done

    if ! $DRY_RUN; then
        rm -f -- "$MANIFEST"
        rmdir --ignore-fail-on-non-empty -- "$STATE_DIR" 2>/dev/null || true
        sysctl --system >/dev/null 2>&1 || true
        udevadm control --reload && udevadm trigger 2>/dev/null || true
    fi
    ok "uninstall complete — system reverted to stock (reboot to drop applied kernel params)."
}

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
main() {
    parse_args "$@"
    require_root
    if $DO_UNINSTALL; then do_uninstall; else do_install; fi
}
main "$@"
