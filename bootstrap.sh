#!/usr/bin/env bash
#
# bootstrap.sh — zero-touch Cachy-Void provisioning for a bare-metal Void host.
#
# Kicks off the whole live install from a single file: verifies the environment,
# derives the kernel tracking state from the running kernel, installs the build
# prerequisites, ensures a void-packages checkout, then hands off to deploy.sh
# and seeds the initial kernel-state.json matrix.
#
# Run as YOUR regular user (not root); it escalates with sudo only where needed.
#
#   ./bootstrap.sh
#   VOID_PACKAGES=/path/to/void-packages ./bootstrap.sh   # override checkout
#
# See INSTALL.md for the full manual.

set -euo pipefail

# ---------------------------------------------------------------------------
# Paths & tunables
# ---------------------------------------------------------------------------
readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly STATE_DIR="/var/lib/cachy-void"                 # engine's Config.state_dir
readonly KERNEL_STATE="$STATE_DIR/kernel/kernel-state.json"
VOID_PACKAGES="${VOID_PACKAGES:-$HOME/void-packages}"

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------
if [ -t 1 ]; then
    C_HDR=$'\033[1;36m'; C_OK=$'\033[0;32m'; C_ERR=$'\033[0;31m'; C_OFF=$'\033[0m'
else
    C_HDR=""; C_OK=""; C_ERR=""; C_OFF=""
fi
step() { printf '\n%s==> %s%s\n' "$C_HDR" "$*" "$C_OFF"; }
ok()   { printf '%s ok %s%s\n'   "$C_OK"  "$*" "$C_OFF"; }
die()  { printf '%serror: %s%s\n' "$C_ERR" "$*" "$C_OFF" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Step 0 — safety: regular user with working sudo
# ---------------------------------------------------------------------------
step "Environment checks"
[ "$(id -u)" -ne 0 ] || die "run as your regular user, not root (this script uses sudo where needed)"
command -v sudo >/dev/null 2>&1 || die "sudo is required but not installed"
# Try non-interactive first: `sudo -v` alone fails under the default
# verifypw=all policy when the user has BOTH a NOPASSWD rule and a passworded
# %wheel entry — even though actual sudo commands would run fine (real-hardware
# finding). Fall back to an interactive prompt for keyboard runs.
sudo -n true 2>/dev/null || sudo -v || die "sudo access is required to provision the system"
BUILD_USER="$(id -un)"
ok "running as $BUILD_USER with sudo access"

# ---------------------------------------------------------------------------
# Step 1 — derive kernel tracking state from the running kernel
# ---------------------------------------------------------------------------
step "Detecting kernel generation"
KVER="$(uname -r)"
BASE_SERIES="$(printf '%s\n' "$KVER" | grep -oE '^[0-9]+\.[0-9]+' || true)"
[ -n "$BASE_SERIES" ] || die "could not derive a base series from 'uname -r' ($KVER)"
ok "running kernel $KVER  ->  tracking series linux$BASE_SERIES (known-good = $KVER)"

# The shipped bore.lock pins specific kernel series. If THIS box's series isn't
# pinned, the first linux-cachy build is withheld (AWAIT_HUMAN_PATCH) until a
# human adds a [[patch]] entry + sha256 (§8.3). Warn early — userspace is unaffected.
BORE_LOCK="$SCRIPT_DIR/updater/bore.lock"
if [ -f "$BORE_LOCK" ] && ! grep -qE "series[[:space:]]*=[[:space:]]*\"$BASE_SERIES\"" "$BORE_LOCK"; then
    printf '%s!! bore.lock has no pin for series %s — the first linux-cachy build will be\n' "$C_ERR" "$BASE_SERIES" >&2
    printf '   WITHHELD until you add a [[patch]] entry (commit + per-series sha256) for it\n' >&2
    printf '   in bore.lock (INSTALL.md §6.2). Userspace updates work regardless.%s\n' "$C_OFF" >&2
fi

# ---------------------------------------------------------------------------
# Step 2 — install build prerequisites
# ---------------------------------------------------------------------------
# NOTE (correction vs. original brief): 'xbps-src' is NOT an installable package
# — it ships inside void-packages (step 3). 'minisign' is not used by this
# project (trust is sha256 via bore.lock, §8.3), so it is intentionally omitted.
# 'base-devel' is required to compile packages with xbps-src.
step "Installing prerequisites (git, xtools, base-devel)"
sudo xbps-install -Sy git xtools base-devel
ok "prerequisites installed"

# ---------------------------------------------------------------------------
# Step 3 — ensure a void-packages checkout + build root
# ---------------------------------------------------------------------------
step "Ensuring void-packages at $VOID_PACKAGES"
if [ ! -d "$VOID_PACKAGES/.git" ]; then
    printf 'void-packages not found at %s — clone it now? [Y/n] ' "$VOID_PACKAGES"
    read -r reply
    case "${reply:-Y}" in
        [Nn]*) die "void-packages is required; set VOID_PACKAGES=<path> and re-run" ;;
    esac
    git clone https://github.com/void-linux/void-packages.git "$VOID_PACKAGES"
    ok "cloned void-packages"
else
    ok "found existing checkout"
fi

# xbps-src must run as a non-root user (which we are). Bootstrap the build root
# only if it has not been initialized yet.
if ! ls -d "$VOID_PACKAGES"/masterdir* >/dev/null 2>&1; then
    step "Preparing the xbps-src build root (binary-bootstrap)"
    ( cd "$VOID_PACKAGES" && ./xbps-src binary-bootstrap )
    ok "build root ready"
fi

# ---------------------------------------------------------------------------
# Step 4 — provision the system (config, engine mirror, GRUB, services)
# ---------------------------------------------------------------------------
# deploy.sh (run as root) installs system config, mirrors the engine to
# /usr/libexec/cachy-void-updater, performs the sanctioned GRUB edits, and
# creates $STATE_DIR/kernel owned by $BUILD_USER.
step "Provisioning system via deploy.sh --with-grub"
sudo bash "$SCRIPT_DIR/deploy.sh" \
    --with-grub \
    --user "$BUILD_USER" \
    --void-packages "$VOID_PACKAGES"
ok "system provisioned"

# ---------------------------------------------------------------------------
# Step 5 — seed the initial kernel-state.json matrix
# ---------------------------------------------------------------------------
# The kernel/ dir was created by deploy.sh owned by $BUILD_USER, so we write the
# state as the regular user (no sudo). Schema matches engine grub.default_state:
# ported_version starts at 0.0.0_0 so the first `--commit` synthesizes and builds
# linux-cachy; known_good pins the currently-running stock kernel as the fallback.
step "Seeding kernel tracking state at $KERNEL_STATE"
[ -d "$(dirname -- "$KERNEL_STATE")" ] || die "state dir missing — did deploy.sh succeed?"
cat > "$KERNEL_STATE" <<JSON
{
  "schema": 1,
  "state": "TRACKING",
  "base_series": "$BASE_SERIES",
  "ported_version": "0.0.0_0",
  "candidate": null,
  "known_good": {
    "kver": "$KVER",
    "grub_ref": null
  },
  "grub": null,
  "bore": null,
  "services_up_at_staging": [],
  "staged_boot_id": null,
  "history": []
}
JSON
ok "kernel-state.json written (base_series=$BASE_SERIES, known_good=$KVER)"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
step "Bootstrap complete"
cat <<EOF

Cachy-Void is provisioned (deploy.sh generated /etc/cachy-void/updater.toml with
defaults — review its [packages] allowlist). Next steps:

  1. Review the generated config (edit the allowlist to taste):
       sudoedit /etc/cachy-void/updater.toml
  2. Pin the BORE patch trust anchor before the first kernel build:
       edit /usr/libexec/cachy-void-updater/bore.lock — fill sha256 + approve (§8.3)
  3. Preview the queue (read-only):
       /usr/libexec/cachy-void-updater/cachy_void_update.py --check --config /etc/cachy-void/updater.toml
  4. Build + deploy (compiles linux-cachy on first run; reboot when prompted):
       sudo -u $BUILD_USER /usr/libexec/cachy-void-updater/cachy_void_update.py --commit --yes

Revert everything with:  sudo $SCRIPT_DIR/deploy.sh --uninstall
EOF
