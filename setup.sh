#!/bin/sh
# Tetrarch one-shot setup. Re-runnable: safe to run any number of times.
#
#   ./setup.sh
#
# Verifies the toolchain, creates .venv with numpy + flask, and builds every
# C source under src/c/ into build/ as a shared library.
#
# No environment-variable feature gates. Every knob here is a visible constant.
set -eu

ROOT=$(cd "$(dirname "$0")" && pwd)
VENV="$ROOT/.venv"
BUILD="$ROOT/build"

CFLAGS_COMMON="-O3 -shared -fPIC -std=c11 -Wall -Wextra"

say() { printf '%s\n' "$*"; }
die() { printf 'setup.sh: %s\n' "$*" >&2; exit 1; }

# --- toolchain ---------------------------------------------------------------

command -v python3 >/dev/null 2>&1 || die "python3 not found"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
    || die "python3 >= 3.10 required, found $(python3 -V 2>&1)"

CC=${CC:-cc}
command -v "$CC" >/dev/null 2>&1 || die "no C compiler ($CC) on PATH"

say "python3: $(python3 -V 2>&1)"
say "cc:      $($CC --version 2>&1 | head -1)"

# Apple clang on arm64 rejects -march=native; GCC/clang on x86 want it. Probe.
ARCHFLAG=""
for flag in -march=native -mcpu=native; do
    if echo 'int main(void){return 0;}' \
        | "$CC" $flag -x c - -o /dev/null 2>/dev/null; then
        ARCHFLAG=$flag
        break
    fi
done
[ -n "$ARCHFLAG" ] || say "warning: neither -march=native nor -mcpu=native accepted"
say "arch:    ${ARCHFLAG:-none}"

# --- python deps -------------------------------------------------------------

[ -d "$VENV" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet numpy flask
say "venv:    $VENV"

# --- C cores -----------------------------------------------------------------

mkdir -p "$BUILD"
built=0
for src in "$ROOT"/src/c/*.c; do
    [ -e "$src" ] || break          # glob did not match: no C sources yet
    name=$(basename "$src" .c)
    out="$BUILD/lib$name.so"
    say "cc       $name.c -> build/lib$name.so"
    # shellcheck disable=SC2086
    "$CC" $CFLAGS_COMMON $ARCHFLAG "$src" -o "$out" -lm
    built=$((built + 1))
done

if [ "$built" -eq 0 ]; then
    say "no C sources under src/c/ yet (expected before Phase 2)"
fi

say ""
say "setup complete. Activate with:  . .venv/bin/activate"
