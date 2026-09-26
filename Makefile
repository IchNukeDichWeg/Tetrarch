# Tetrarch. `./setup.sh` is the real build; this is the conventional front door.
#
#   make            build the C core
#   make test       the selftest ladder
#   make quick      build, then only the movegen/C-core sections -- the inner
#                   loop, ~8s against ~80s. NOT the gate; run `make test`
#                   before committing. `selftest.py --only SUBSTR` picks others.
#   make bench      the bench signature
#   make dist       source tarball for a release
#   make dist REF=v3   ...from a tag instead of the working tree
#   make clean

VERSION := $(shell python3 -c "import re; print(re.search(r'VERSION = \"(.*)\"', open('uci.py').read()).group(1))")
REF     ?= HEAD
NAME    := tetrarch-v$(VERSION)

.PHONY: all build test quick bench dist clean

all: build

build:
	./setup.sh --no-test

test:
	python3 selftest.py

# What an edit to src/c/ actually needs to hear: both perfts and the C-core
# node-for-node agreement. Measured on an 18-core M4: 8.0s / 139 checks
# against 82.5s / 664 for the full ladder, because repetition (28.9s), search
# (18.2s) and uci replay (16.6s) are ~75% of it and none of them is what a
# movegen change breaks first. The random cross-check is only 2.8s, so
# dropping THAT buys nothing -- which is why this filters sections instead.
# Run `make test` before committing -- this is the loop, not the gate.
quick: build
	python3 selftest.py --only perft,c_core,make_unmake,rotation

bench:
	@printf "bench\nquit\n" | python3 uci.py | tail -1

# git archive, so a release tarball is exactly the tree at that ref -- no build
# output, no .venv, nothing that was not committed.
dist:
	@if [ "$(REF)" = "HEAD" ]; then out="$(NAME).tar.gz"; else out="tetrarch-$(REF).tar.gz"; fi; \
	prefix=$$(basename $$out .tar.gz); \
	git archive --format=tar.gz --prefix=$$prefix/ $(REF) -o $$out && \
	echo "wrote $$out ($$(du -h $$out | cut -f1))"

clean:
	rm -rf build __pycache__ */__pycache__
	rm -f tetrarch-v*.tar.gz
