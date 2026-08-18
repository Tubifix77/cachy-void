#!/bin/sh
#
# get.sh — Cachy-Void one-line installer bootstrap.
#
# From a completely fresh Void install (xbps-fetch ships with xbps itself, so
# this needs nothing pre-installed):
#
#   xbps-fetch https://raw.githubusercontent.com/Tubifix77/cachy-void/main/get.sh && sh get.sh
#
# Or, if curl happens to be installed already:
#
#   curl -fsSL https://raw.githubusercontent.com/Tubifix77/cachy-void/main/get.sh | sh
#
# Arguments are forwarded through bootstrap.sh to deploy.sh, e.g.:
#
#   sh get.sh --with-networkmanager --with-branding
#   curl -fsSL .../get.sh | sh -s -- --with-networkmanager
#
# What it does: installs git (sudo xbps-install) if missing, clones the
# repository to ~/cachy-void (override with CACHY_VOID_DIR), then hands off to
# bootstrap.sh, which provisions everything end to end. The clone is permanent
# by design — it is the overlay's source tree and your uninstaller
# (`sudo ~/cachy-void/deploy.sh --uninstall`); do not delete it after install.
#
# The whole script is wrapped in main() and only invoked on the last line, so
# a truncated download executes nothing.

set -eu

if [ -t 1 ]; then
    C_HDR='\033[1;36m'; C_ERR='\033[0;31m'; C_OFF='\033[0m'
else
    C_HDR=''; C_ERR=''; C_OFF=''
fi
say()  { printf '%b==> get.sh:%b %s\n' "$C_HDR" "$C_OFF" "$*"; }
fail() { printf '%berror:%b %s\n' "$C_ERR" "$C_OFF" "$*" >&2; exit 1; }

main() {
    # -- guards --------------------------------------------------------------
    if ! command -v xbps-install >/dev/null 2>&1; then
        fail "this installer is for Void Linux (xbps-install not found)"
    fi
    if [ "$(id -u)" -eq 0 ]; then
        fail "run as your regular user, not root (sudo is used where needed)"
    fi

    # When piped (curl | sh) stdin is the script itself; rebind it to the
    # terminal so interactive prompts (sudo password, bootstrap's [Y/n])
    # read from the keyboard instead of eating the script.
    if [ ! -t 0 ] && [ -r /dev/tty ]; then
        exec < /dev/tty
    fi

    repo="${CACHY_VOID_REPO:-https://github.com/Tubifix77/cachy-void.git}"
    dir="${CACHY_VOID_DIR:-$HOME/cachy-void}"

    # -- git -----------------------------------------------------------------
    if ! command -v git >/dev/null 2>&1; then
        say "installing git (sudo xbps-install -Sy git)"
        sudo xbps-install -Sy git
    fi

    # -- clone or refresh ----------------------------------------------------
    if [ -d "$dir/.git" ]; then
        say "existing checkout at $dir — refreshing (git pull --ff-only)"
        git -C "$dir" pull --ff-only
    elif [ -e "$dir" ]; then
        fail "$dir exists but is not a git checkout — move it aside or set CACHY_VOID_DIR"
    else
        say "cloning $repo -> $dir"
        git clone "$repo" "$dir"
    fi

    # -- hand off -------------------------------------------------------------
    say "handing off to bootstrap.sh (INSTALL.md documents every step)"
    cd "$dir"
    exec bash ./bootstrap.sh "$@"
}

main "$@"
