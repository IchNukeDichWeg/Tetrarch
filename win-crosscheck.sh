#!/usr/bin/env bash
# Cross-build the C core for Windows and check it is USABLE, without a Windows machine.
#
# Two things break a Windows build of this engine and neither shows up on macOS or Linux:
#   1. a POSIX call with no mingw equivalent (tetrarch.c uses clock_gettime, which mingw-w64 has)
#   2. a symbol ctypes needs that the DLL does not export -- Windows DLLs export NOTHING by default,
#      and MinGW's auto-export only applies while no symbol is marked __declspec(dllexport). Add one
#      and every other export silently disappears.
#
# So: build it, then diff the DLL's export table against the names core.py actually looks up.
#
#   brew install mingw-w64     # or: apt install mingw-w64
#   ./win-crosscheck.sh
#
# This is a CHECK, not the shipping route -- a real install builds natively under MSYS2 with
# -march=native. Cross-building cannot use that, so an explicit ISA is passed here instead.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")" && pwd)
CC=x86_64-w64-mingw32-gcc
OBJDUMP=x86_64-w64-mingw32-objdump

command -v "$CC" >/dev/null 2>&1 || {
    echo "no $CC -- install mingw-w64 (brew install mingw-w64)"; exit 1; }

OUT=$(mktemp -d)
trap 'rm -rf "$OUT"' EXIT
DLL="$OUT/tetrarch.dll"

echo "cc        src/c/tetrarch.c -> tetrarch.dll (x86-64, AVX2)"
"$CC" -O3 -shared -std=c11 -Wall -Wextra -mavx2 "$ROOT/src/c/tetrarch.c" -o "$DLL" -lm

# every tt_* the binding resolves must be in the export table
"$OBJDUMP" -p "$DLL" | awk '/Ordinal Base/,0' | grep -oE '\btt_[a-z_0-9]+' | sort -u > "$OUT/have"
grep -oE 'lib\.[a-z_0-9]+' "$ROOT/tetrarch/core.py" | sed 's/^lib\.//' | sort -u > "$OUT/want"
missing=$(comm -23 "$OUT/want" "$OUT/have")

if [ -n "$missing" ]; then
    echo "!! these are looked up by tetrarch/core.py but NOT exported by the DLL:"
    echo "$missing" | sed 's/^/     /'
    exit 1
fi
echo "ok        $(wc -l < "$OUT/want" | tr -d ' ') symbols exported, none missing"
