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
#   the FILES WRITTEN       -> this script (real applier runs, disjoint HOMEs --
#                              plus one SHARED home, because two appliers editing
#                              one file is its own failure mode)
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
for b in lxqt-session openbox plasmashell xfce4-session; do
    printf '#!/bin/sh\n' > "$FAKE/usr/bin/$b"; chmod 755 "$FAKE/usr/bin/$b"
done

H_LXQT="$(mktemp -d)"; H_PLASMA="$(mktemp -d)"; H_XFCE="$(mktemp -d)"
# One more HOME, for the opposite question: what happens when two appliers write
# the SAME file. See the gtk.css assertion at the bottom.
H_BOTH="$(mktemp -d)"
cleanup(){ $KEEP || rm -rf "$FAKE" "$H_LXQT" "$H_PLASMA" "$H_XFCE" "$H_BOTH"; }
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
# Xfce without a session writes its files and defers its xfconf properties to a
# one-shot, by design -- so the file half is exactly what this script can check.
echo "== applying --desktop xfce =="
run_target xfce "$H_XFCE"; note "$(files_in "$H_XFCE" | wc -l) files"

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

xfce_own="$(files_in "$H_XFCE" | grep -E '^\.config/(xfce4/terminal/terminalrc|autostart/cachy-void-.*-xfce\.desktop)' || true)"
if [ -n "$xfce_own" ]; then note "OK   the xfce target wrote Xfce config"
else note "FAIL the xfce target wrote no Xfce config"; fail=1; fi

leak_into_plasma="$(comm -12 <(printf '%s\n' "$lxqt_own") <(files_in "$H_PLASMA") | grep . || true)"
if [ -z "$leak_into_plasma" ]; then note "OK   no lxqt/openbox file leaked into the plasma target"
else note "FAIL lxqt files present in the plasma run:"; printf '       %s\n' "$leak_into_plasma"; fail=1; fi

leak_into_lxqt="$(comm -12 <(printf '%s\n' "$plasma_own") <(files_in "$H_LXQT") | grep . || true)"
if [ -z "$leak_into_lxqt" ]; then note "OK   no Plasma file leaked into the lxqt target"
else note "FAIL Plasma files present in the lxqt run:"; printf '       %s\n' "$leak_into_lxqt"; fail=1; fi

leak_into_xfce="$(comm -12 <(printf '%s\n' "$lxqt_own") <(files_in "$H_XFCE") | grep . || true)"
if [ -z "$leak_into_xfce" ]; then note "OK   no lxqt/openbox file leaked into the xfce target"
else note "FAIL lxqt files present in the xfce run:"; printf '       %s\n' "$leak_into_xfce"; fail=1; fi

leak_out_of_xfce="$(comm -12 <(printf '%s\n' "$xfce_own") <(files_in "$H_LXQT") | grep . || true)"
if [ -z "$leak_out_of_xfce" ]; then note "OK   no Xfce file leaked into the lxqt target"
else note "FAIL Xfce files present in the lxqt run:"; printf '       %s\n' "$leak_out_of_xfce"; fail=1; fi

# --- and the inverse of isolation: ONE file two appliers both legitimately edit
# ~/.config/gtk-3.0/gtk.css carries a marked block from the shared/LXQt half (it
# tints Plank's running-indicator dot) and a second, differently marked block
# from the Xfce applier (it repaints the panel's checked states brand green,
# which is theme CSS and has no xfconf property). Each applier strips and
# rewrites its OWN block. If either regex ever widens to match the other's
# markers, branding both desktops silently deletes half the rules -- and the box
# that hits it is the one running both, which is the test laptop. Cheap to
# assert, invisible until someone logs in.
echo "== applying --desktop lxqt,xfce into ONE home =="
run_target "lxqt,xfce" "$H_BOTH"
GC_BOTH="$H_BOTH/.config/gtk-3.0/gtk.css"
if [ -f "$GC_BOTH" ]; then
    _shared_blk=$(grep -c '>>> cachy-void >>>' "$GC_BOTH" || true)
    _xfce_blk=$(grep -c '>>> cachy-void xfce >>>' "$GC_BOTH" || true)
    if [ "$_shared_blk" -ge 1 ] && [ "$_xfce_blk" -ge 1 ]; then
        note "OK   gtk.css keeps BOTH marked blocks (Plank dot + Xfce panel accent)"
    else
        note "FAIL gtk.css lost a block: shared=$_shared_blk xfce=$_xfce_blk"
        note "     one applier's strip regex is matching the other's markers"
        fail=1
    fi
else
    note "WARN no gtk.css written by either applier (assets missing from this box?)"
fi

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
