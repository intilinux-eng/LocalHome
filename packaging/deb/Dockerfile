# The room the .deb is built in: the board's own Debian (trixie, Python 3.13),
# for the board's processor (arm64, through QEMU on a PC) - so what's compiled
# here runs there. Nothing of it ends up on the board but the package.
# Debian's own image for that processor (arm64v8/debian, amd64/debian): one
# tag per processor, so Docker never takes the other one's from its cache.
ARG BASE=arm64v8/debian:trixie
FROM ${BASE}

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-venv python3-dev gcc libc6-dev dpkg-dev ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Cython turns LocalHome's modules into compiled libraries; it stays here.
RUN python3 -m venv /tools \
 && /tools/bin/pip install --no-cache-dir "cython>=3.1,<3.2" "setuptools>=75"

COPY build.sh /usr/local/bin/build-localhome-deb
ENTRYPOINT ["sh", "/usr/local/bin/build-localhome-deb"]
