# The machine that builds LocalHome's .deb (tools/build_deb.py runs it):
# arm64 - the Arduino UNO Q's - emulated by QEMU on a PC, and Debian
# trixie like the board, so the program finds the same C library and
# Python 3.13 it was built against. pip only here, never on the board.
FROM --platform=linux/arm64 debian:trixie

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-dev python3-venv gcc g++ ccache patchelf dpkg-dev curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# LocalHome's dependencies at the versions tested together, and Nuitka:
# in the image, so a build only compiles.
COPY pyproject.toml constraints.txt /deps/
RUN python3 -m venv /venv \
    && /venv/bin/pip install --no-cache-dir --upgrade pip \
    && /venv/bin/python -c "import tomllib; print('\n'.join(tomllib.load(open('/deps/pyproject.toml', 'rb'))['project']['dependencies']))" > /deps/requirements.txt \
    && /venv/bin/pip install --no-cache-dir -r /deps/requirements.txt -c /deps/constraints.txt \
    && /venv/bin/pip install --no-cache-dir "nuitka>=2.7,<3" ordered-set

ENV NUITKA_CACHE_DIR=/cache/nuitka CCACHE_DIR=/cache/ccache
