# Tetrarch. `./setup.sh` is the real build; this is the conventional front door.
#
#   make            build the C core
#   make test       the selftest ladder
#   make bench      the bench signature
#   make dist       source tarball for a release
#   make dist REF=v3   ...from a tag instead of the working tree
#   make clean

VERSION := $(shell python3 -c "import re; print(re.search(r'VERSION = \"(.*)\"', open('uci.py').read()).group(1))")
REF     ?= HEAD
NAME    := tetrarch-v$(VERSION)

.PHONY: all build test bench dist clean

all: build

build:
	./setup.sh --no-test

test:
	python3 selftest.py

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
