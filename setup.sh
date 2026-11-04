#!/bin/sh
# Tetrarch one-shot setup. Re-runnable: safe to run any number of times.
#
#   ./setup.sh              install what is missing, build, run the selftest
#   ./setup.sh --no-install build and test only; never touch the system
#   ./setup.sh --no-test    build only; skip the selftest (fast rebuild loop)
#
# A fresh A/B box is `git clone` + `./setup.sh`, so this installs the toolchain
# rather than merely complaining that it is absent. It ends by running the whole
# selftest ladder, because a setup that "succeeded" and left a broken .so is
# worse than one that failed -- and on a new architecture the ladder is the only
# thing that proves the C core still agrees with the Python reference.
#
# No virtualenv: numpy and flask go into the system python3, so every command
# in this repo is a plain `python3 something.py`. On a distro python that is
# marked externally managed (PEP 668) pip needs --break-system-packages, which
# this adds only after a plain install has been refused. That is the trade: two
# packages can now collide with OS-managed ones. On a disposable A/B box that
# is fine; on a machine you care about, `./setup.sh --no-install` and install
# numpy and flask however you prefer.
#
# No environment-variable feature gates. Every knob here is a visible constant
# or a flag.
set -eu

ROOT=$(cd "$(dirname "$0")" && pwd)
BUILD="$ROOT/build"

CFLAGS_COMMON="-O3 -shared -fPIC -std=c11 -Wall -Wextra"
# Windows (MSYS2/mingw-w64) builds a DLL, not a .so, and -fPIC is meaningless
# there -- everything is position-independent, and gcc warns about being told so.
# The rest of the flags carry over unchanged: the one POSIX call in tetrarch.c
# (clock_gettime) is provided by mingw-w64, and MinGW auto-exports every symbol
# when nothing is marked __declspec(dllexport), so ctypes finds them all.
case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*)
        CFLAGS_COMMON="-O3 -shared -std=c11 -Wall -Wextra"
        LIB_PREFIX=""; LIB_EXT=".dll" ;;
    *)  LIB_PREFIX="lib"; LIB_EXT=".so" ;;
esac
MIN_PYTHON="3.10"
PYTHON_DEPS="numpy flask"

NO_INSTALL=0
NO_TEST=0
for arg in "$@"; do
    case "$arg" in
        --no-install) NO_INSTALL=1 ;;
        --no-test) NO_TEST=1 ;;
        -h|--help) sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) printf 'setup.sh: unknown option %s\n' "$arg" >&2; exit 2 ;;
    esac
done

say() { printf '%s\n' "$*"; }
warn() { printf 'setup.sh: %s\n' "$*" >&2; }
die() { printf 'setup.sh: %s\n' "$*" >&2; exit 1; }

# --- platform ----------------------------------------------------------------

OS=$(uname -s)
SUDO=""
if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
fi

PM=""
for candidate in apt-get dnf yum apk pacman brew; do
    if command -v "$candidate" >/dev/null 2>&1; then
        PM=$candidate
        break
    fi
done
say "platform: $OS $(uname -m)${PM:+ (${PM})}"

# `brew` must never run under sudo, and refuses to anyway.
[ "$PM" = "brew" ] && SUDO=""

install_packages() {
    if [ "$NO_INSTALL" -eq 1 ]; then
        warn "missing: $*  (--no-install, so not installing)"
        return 1
    fi
    if [ -z "$PM" ]; then
        warn "no known package manager; install manually: $*"
        return 1
    fi
    say "installing: $*"
    case "$PM" in
        apt-get) $SUDO apt-get update -qq && $SUDO apt-get install -y -qq "$@" ;;
        dnf)     $SUDO dnf install -y -q "$@" ;;
        yum)     $SUDO yum install -y -q "$@" ;;
        apk)     $SUDO apk add --quiet "$@" ;;
        pacman)  $SUDO pacman -Sy --noconfirm --quiet "$@" ;;
        brew)    brew install "$@" ;;
    esac
}

packages_for() {
    case "$PM:$1" in
        apt-get:compiler) echo "build-essential" ;;
        apt-get:pip)      echo "python3-pip python3-dev" ;;
        apt-get:python)   echo "python3" ;;
        dnf:compiler|yum:compiler) echo "gcc make" ;;
        dnf:pip|yum:pip)           echo "python3-pip python3-devel" ;;
        dnf:python|yum:python)     echo "python3" ;;
        apk:compiler)     echo "build-base" ;;
        apk:pip)          echo "py3-pip python3-dev" ;;
        apk:python)       echo "python3" ;;
        pacman:compiler)  echo "base-devel" ;;
        pacman:pip)       echo "python-pip" ;;
        pacman:python)    echo "python" ;;
        brew:compiler)    echo "" ;;   # Xcode command line tools, not brew
        brew:pip)         echo "" ;;
        brew:python)      echo "python@3.12" ;;
        *) echo "" ;;
    esac
}

ensure() {
    # ensure <package-key> <test-command...>
    key=$1
    shift
    if "$@" >/dev/null 2>&1; then
        return 0
    fi
    pkgs=$(packages_for "$key")
    if [ -n "$pkgs" ]; then
        # shellcheck disable=SC2086
        install_packages $pkgs || true
    fi
    "$@" >/dev/null 2>&1
}

# --- python ------------------------------------------------------------------

ensure python command -v python3 || die "python3 not found and could not be installed"
python3 -c "import sys; sys.exit(0 if sys.version_info >= tuple(int(p) for p in '$MIN_PYTHON'.split('.')) else 1)" \
    || die "python3 >= $MIN_PYTHON required, found $(python3 -V 2>&1)"
say "python:   $(python3 -V 2>&1) at $(command -v python3)"

# --- compiler ----------------------------------------------------------------

CC=${CC:-cc}
if ! command -v "$CC" >/dev/null 2>&1; then
    pkgs=$(packages_for compiler)
    if [ -n "$pkgs" ]; then
        # shellcheck disable=SC2086
        install_packages $pkgs || true
    fi
fi
command -v "$CC" >/dev/null 2>&1 || CC=gcc
command -v "$CC" >/dev/null 2>&1 || CC=clang
command -v "$CC" >/dev/null 2>&1 || die \
    "no C compiler. On macOS run: xcode-select --install"
say "cc:       $($CC --version 2>&1 | head -1)"

# Probe rather than assume, and carry on unoptimised if neither works. ORDER
# MATTERS and differs by architecture:
#
#   arm64: clang now ACCEPTS -march=native instead of rejecting it, and resolves
#   it to -target-cpu apple-m1 -- the same CPU it picks with no flag at all.
#   Only -mcpu=native names the real core (verified with `cc -### -c`: apple-m1
#   vs apple-m4 on an M4). A probe that tries -march first therefore succeeds
#   and silently tunes for the wrong chip.
#
#   x86: -mcpu=native is the deprecated spelling of -mtune=native and selects
#   NO instruction set, so -march must stay first there.
case "$(uname -m)" in
    arm64|aarch64) ARCH_PROBE="-mcpu=native -march=native" ;;
    *)             ARCH_PROBE="-march=native -mcpu=native" ;;
esac
ARCHFLAG=""
for flag in $ARCH_PROBE; do
    if echo 'int main(void){return 0;}' \
        | "$CC" $flag -x c - -o /dev/null 2>/dev/null; then
        ARCHFLAG=$flag
        break
    fi
done
[ -n "$ARCHFLAG" ] || warn "neither -march=native nor -mcpu=native accepted; \
building without architecture tuning"
say "arch:     ${ARCHFLAG:-none}"

# -fno-semantic-interposition: a -fPIC library with default visibility lets
# the dynamic linker swap any exported function for another object's, so GCC
# must route every call from one tt_* to another through the PLT and may not
# inline across them. In this file that was 197 call sites -- tt_make,
# tt_unmake, tt_is_attacked, tt_gen_pseudo, tt_see from inside the search --
# each an extra indirect jump through the GOT, and tt_in_check/tt_eval never
# inlined. With the flag every one is a direct call (objdump: 197 @plt -> 0).
# Nothing loads this library to interpose on it; ctypes looks names up with
# dlsym, which still finds them all because the symbols stay exported. It is
# a pure code-generation change: callgrind Ir in libtetrarch for one bench
# round 3,417.8M -> 3,415.0M (-0.08%, gcc 13, -march=x86-64-v3), signature
# unchanged. Small, because GCC would not inline the big ones anyway; it is
# here because it is free, not because it is a large win.
#
# Probed with -Werror so a compiler that ignores it with a warning (or a clang
# too old to know it) builds without it instead of warning on every build.
# PE/Mach-O have no ELF interposition; the flag is harmless there.
#
# Measured on the same box and NOT adopted (Ir as above, one bench round;
# CPU time paired over 16 reps had a null IQR of +-12%, so none of the time
# figures below is resolvable and none is claimed):
#   -O2                       +35.6% Ir: the NNUE loops stop vectorising.
#   -funroll-loops            -3.8% Ir, +19% code size, time -1.8% (null -1.8%).
#   -flto                     +0.02% Ir; one translation unit, nothing to link.
#   visibility(protected)     `#pragma GCC visibility push(protected)` after
#                             the includes gives the same code as this flag
#                             and would also cover hand builds, but is
#                             ELF-only and a source edit for a build concern.
#                             -fvisibility=hidden alone does nothing here:
#                             the API ctypes needs stays default, so stays
#                             interposable.
#   -fno-plt                  moot once internal calls are direct; the PLT is
#                             left with cold libc calls (log, clock_gettime).
#   -fno-stack-protector      -0.25% Ir more (Ubuntu gcc turns it on); not
#                             worth dropping a hardening default for.
#   clang 18 instead of gcc   -10.1% Ir, yet paired time -7.3% (SLOWER, inside
#                             the null): fewer instructions is not less time.
#   gcc PGO                   -4.7% Ir, time +4.7% median (inside the null);
#                             the same Ir when trained on positions the bench
#                             does not contain, so it is not overfit -- but it
#                             needs a training run inside setup, a different
#                             toolchain on clang (whose profile runtime is not
#                             even installed on stock Ubuntu), and it makes
#                             every A/B compare two different profiles.
SEMFLAG=""
if echo 'int main(void){return 0;}' \
    | "$CC" -Werror -fno-semantic-interposition -x c - -o /dev/null 2>/dev/null; then
    SEMFLAG="-fno-semantic-interposition"
fi
say "interpos: ${SEMFLAG:-default (flag not accepted)}"

# --- python packages ---------------------------------------------------------

missing=""
for pkg in $PYTHON_DEPS; do
    python3 -c "import $pkg" >/dev/null 2>&1 || missing="$missing $pkg"
done

if [ -n "$missing" ] && [ "$NO_INSTALL" -eq 0 ]; then
    ensure pip python3 -m pip --version \
        || die "pip is unavailable; install python3-pip and re-run"
    # shellcheck disable=SC2086
    if ! python3 -m pip install --quiet --user --no-warn-script-location $missing 2>/dev/null; then
        # PEP 668: distro and Homebrew pythons refuse to be written to without
        # this. Only reached after the polite attempt has already failed.
        say "python:   externally managed, retrying with --break-system-packages"
        # shellcheck disable=SC2086
        python3 -m pip install --quiet --user --break-system-packages \
            --no-warn-script-location $missing \
            || python3 -m pip install --quiet --break-system-packages \
               --no-warn-script-location $missing \
            || die "could not install:$missing"
    fi
elif [ -n "$missing" ]; then
    warn "missing python packages:$missing  (--no-install)"
fi

for pkg in $PYTHON_DEPS; do
    python3 -c "import $pkg" >/dev/null 2>&1 \
        || die "$pkg is still not importable by $(command -v python3)"
done
say "deps:     numpy $(python3 -c 'import numpy; print(numpy.__version__)'), \
flask $(python3 -c 'import flask, importlib.metadata as m; print(m.version("flask"))')"

# --- C cores -----------------------------------------------------------------

mkdir -p "$BUILD"
built=0
for src in "$ROOT"/src/c/*.c; do
    [ -e "$src" ] || break          # glob did not match: no C sources yet
    name=$(basename "$src" .c)
    out="$BUILD/$LIB_PREFIX$name$LIB_EXT"
    say "cc        $name.c -> build/$LIB_PREFIX$name$LIB_EXT"
    # Drop the previous library FIRST. cc writes no output when it fails, so
    # without this a compile error leaves the last good build in place and the
    # next `python3 selftest.py` silently measures code that is no longer in
    # src/c/. Better to have nothing than to have yesterday's engine.
    rm -f "$out"
    # shellcheck disable=SC2086
    "$CC" $CFLAGS_COMMON $ARCHFLAG $SEMFLAG "$src" -o "$out" -lm
    built=$((built + 1))
done
[ "$built" -eq 0 ] && say "no C sources under src/c/ yet"

# --- git identity ------------------------------------------------------------
#
# A rented A/B box has to commit and push the campaign state file the turn a
# tranche ends. A fresh clone has no identity, and git then invents one from
# hostname and login -- which would put a real machine name into a public repo.

if [ -d "$ROOT/.git" ] && ! git -C "$ROOT" config user.email >/dev/null 2>&1; then
    git -C "$ROOT" config user.name "IchNukeDichWeg"
    git -C "$ROOT" config user.email \
        "98532405+IchNukeDichWeg@users.noreply.github.com"
    say "git:      set repo-local identity (was unset)"
fi

# --- verify ------------------------------------------------------------------
#
# Run the whole ladder. A setup that reports success and leaves a broken build
# costs a campaign to discover, and on a fresh box the ladder is also the only
# thing that proves the compiler and the C core agree with the Python reference
# on this architecture.

if [ "$NO_TEST" -eq 1 ]; then
    say ""
    say "skipping selftest (--no-test)"
elif [ "$built" -gt 0 ]; then
    say ""
    ( cd "$ROOT" && python3 -c 'from tetrarch import core; core.load()' ) \
        || die "the library just built will not load"
    ( cd "$ROOT" && python3 selftest.py ) \
        || die "selftest failed; do not run a campaign with this build"
fi

say ""
say "setup complete."
