#!/usr/bin/env bash
# dispatch-isolation.sh — prove that branding ONE desktop writes only that
# desktop's files, without a desktop, a session, or installed assets.
#
# Why this exists: the obvious system test is a clean Void with both desktops,
# run deploy.sh choosing one, check, wipe, repeat choosing both. That is a lot of
# machine time to check a decision, and it still cannot check the part that needs
# eyes. Split the question instead:
#
#   the DECISION            -> tests/test_de_detect.py + test_branding_dispatch.py
#                              (pure logic, fake filesystem, runs anywhere)
#   the FILES WRITTEN       -> this script (real applier runs, disjoint HOMEs)
#   the PIXELS              -> a real login. Nothing offline can do this.
#
# Runs fine in the Void WSL sandbox. Two prerequisites, because of what the
# appliers legitimately require:
#   * a NON-ROOT user: a real applier run refuses root, correctly, since the
#     config it writes is per-user. Void in WSL is root, so pass --user NAME (it
#     is created if missing) or run this as an ordinary user.
#   * kwriteconfig6 (package kf6-kconfig) for the Plasma half. Without it the
#     Plasma applier exits early by design and that half is skipped, not failed.
#
# Everything else degrades honestly: no KvArcDark means no Kvantum skin, no
# ImageMagick means no rendered wallpaper. Those are warnings, not failures, and
# the files this checks are written regardless.
set -uo pipefail

REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
APPLIER="$REPO/system/bin/cachy-branding"
ASSETS="$REPO/system/branding"
USER_ARG=""
KEEP=false

while [ $# -gt 0 ]; do
    case "$1" in
        --user) USER_ARG="${2:?--user needs a name}"; shift 2 ;;
        --keep) KEEP=true; shift ;;
        -h|--help) sed -n '2,30p' "$0" | sed 's/^#\( \|$\)//'; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

[ -x "$APPLIER" ] || [ -f "$APPLIER" ] || { echo "applier not found at $APPLIER" >&2; exit 1; }

fail=0
note(){ printf '  %s\n' "$*"; }

# --- a fake / so the detector reports both desktops without either installed ---
FAKE="$(mktemp -d)"
mkdir -p "$FAKE/usr/bin"
for b in lxqt-session openbox plasmashell; do
    printf '#!/bin/sh\n' > "$FAKE/usr/bin/$b"; chmod 755 "$FAKE/usr/bin/$b"
done

H_LXQT="$(mktemp -d)"; H_PLASMA="$(mktemp -d)"
cleanup(){ $KEEP || rm -rf "$FAKE" "$H_LXQT" "$H_PLASMA"; }
trap cleanup EXIT

# --- run the real applier for one target, into its own HOME -------------------
run_target() {   # $1 = target, $2 = HOME
    # A clean PATH, not the inherited one: under WSL the environment carries the
    # Windows PATH, `su` re-parses this string with /bin/sh, and "Program Files
    # (x86)" makes dash die on the parenthesis. Nothing here needs more than this.
    local env_pfx="HOME=$2 CACHY_BRANDING_ASSETS=$ASSETS CACHY_DE_ROOT=$FAKE"
    env_pfx="$env_pfx CACHY_DE_PATH=/usr/bin PATH=$REPO/system/bin:/usr/local/bin:/usr/bin:/bin"
    if [ "$(id -u)" -eq 0 ]; then
        if [ -z "$USER_ARG" ]; then
            echo "running as root and no --user given: a real applier run refuses root." >&2
            echo "re-run with --user NAME (created if missing)." >&2
            exit 2
        fi
        id "$USER_ARG" >/dev/null 2>&1 || useradd -m -s /bin/sh "$USER_ARG"
        chown -R "$USER_ARG" "$2"
        su "$USER_ARG" -c "$env_pfx bash $APPLIER --desktop $1" >"$2/.applier.log" 2>&1
    else
        env HOME="$2" CACHY_BRANDING_ASSETS="$ASSETS" CACHY_DE_ROOT="$FAKE" \
            CACHY_DE_PATH=/usr/bin PATH="$REPO/system/bin:$PATH" \
            bash "$APPLIER" --desktop "$1" >"$2/.applier.log" 2>&1
    fi
}

files_in(){ ( cd "$1" 2>/dev/null && find . -type f ! -name '.applier.log' | sed 's|^\./||' | sort ); }

echo "== applying --desktop lxqt =="
run_target lxqt "$H_LXQT";   note "$(files_in "$H_LXQT" | wc -l) files"
echo "== applying --desktop plasma =="
run_target plasma "$H_PLASMA"; note "$(files_in "$H_PLASMA" | wc -l) files"

# --- the assertions ----------------------------------------------------------
echo
echo "== assertions =="

lxqt_own="$(files_in "$H_LXQT" | grep -E '^\.config/(lxqt|openbox)/' || true)"
plasma_own="$(files_in "$H_PLASMA" | grep -E 'kdeglobals|plasmarc|color-schemes/|desktoptheme/' || true)"

if [ -n "$lxqt_own" ]; then note "OK   the lxqt target wrote lxqt/openbox config"
else note "FAIL the lxqt target wrote no lxqt/openbox config"; fail=1; fi

if [ -n "$plasma_own" ]; then note "OK   the plasma target wrote Plasma config"
elif ! command -v kwriteconfig6 >/dev/null 2>&1; then
    note "SKIP Plasma half: kwriteconfig6 not installed (xbps-install -S kf6-kconfig)"
else note "FAIL the plasma target wrote no Plasma config"; fail=1; fi

leak_into_plasma="$(comm -12 <(printf '%s\n' "$lxqt_own") <(files_in "$H_PLASMA") | grep . || true)"
if [ -z "$leak_into_plasma" ]; then note "OK   no lxqt/openbox file leaked into the plasma target"
else note "FAIL lxqt files present in the plasma run:"; printf '       %s\n' "$leak_into_plasma"; fail=1; fi

leak_into_lxqt="$(comm -12 <(printf '%s\n' "$plasma_own") <(files_in "$H_LXQT") | grep . || true)"
if [ -z "$leak_into_lxqt" ]; then note "OK   no Plasma file leaked into the lxqt target"
else note "FAIL Plasma files present in the lxqt run:"; printf '       %s\n' "$leak_into_lxqt"; fail=1; fi

# Tier 1 is SUPPOSED to be in both - assert that too, so a future "fix" that makes
# the targets disjoint by dropping shared assets gets caught.
shared="$(comm -12 <(files_in "$H_LXQT") <(files_in "$H_PLASMA") | grep -E 'qterminal|fastfetch|cachy-void' || true)"
if [ -n "$shared" ]; then note "OK   shared Tier-1 assets present in both targets"
else note "WARN no shared Tier-1 asset found in both (assets missing from this box?)"; fi

echo
if [ "$fail" -eq 0 ]; then echo "PASS - branding one desktop writes only that desktop's files."
else echo "FAIL - see above."; fi
$KEEP && echo "(kept: $H_LXQT $H_PLASMA)"
exit "$fail"
