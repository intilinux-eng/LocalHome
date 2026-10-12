#!/bin/sh
# Builds LocalHome's .deb inside packaging/Dockerfile's machine - run by
# tools/build_deb.py, which mounts:
#   /src    the commit to build, as git archive gives it (committed work only)
#   /out    where the .deb lands
#   /cache  Nuitka's and ccache's caches, kept between builds: the next
#           build compiles only what changed
#
#   sh /src/packaging/build.sh <version> <commit> <date>
set -eu
VERSION=$1
COMMIT=$2
DATE=$3
WORK=/work
SMOKE=/tmp/smoke
PORT=5000

rm -rf "$WORK"
mkdir -p "$WORK" /out "$NUITKA_CACHE_DIR" "$CCACHE_DIR"
cp -a /src/src "$WORK/src"
# What the admin panel shows as the running version (localhome/runtime.py).
printf '{"version": "%s", "commit": "%s", "date": "%s"}\n' "$VERSION" "$COMMIT" "$DATE" \
    > "$WORK/src/localhome/version.json"

echo "== Nuitka: compiling LocalHome $VERSION ($(nproc) cores)"
start=$(date +%s)
cd "$WORK"
PYTHONPATH="$WORK/src" /venv/bin/python -m nuitka \
    --standalone \
    --output-dir="$WORK/build" \
    --output-filename=localhome \
    --include-package=localhome \
    --include-package-data=localhome \
    --nofollow-import-to=pytest,_pytest,playwright \
    --jobs="$(nproc)" \
    --assume-yes-for-downloads \
    ${NUITKA_EXTRA:-} \
    "$WORK/src/localhome"
DIST=$(ls -d "$WORK"/build/*.dist)
echo "   compiled in $(( $(date +%s) - start )) s: $(du -sh "$DIST" | cut -f1)"

echo "== Smoke test: the program starts and its pages answer"
rm -rf "$SMOKE"
mkdir -p "$SMOKE"
cp /src/config/config.climate.example.yaml "$SMOKE/config.yaml"
printf '{"host": "localhost", "port": 1883}\n' > "$SMOKE/mqtt_broker.json"
LOCALHOME_CONFIG="$SMOKE/config.yaml" "$DIST/localhome" > "$SMOKE/log" 2>&1 &
pid=$!
ok=0
for i in $(seq 1 180); do
    if curl -fsS "http://127.0.0.1:$PORT/api/server-time" > /dev/null 2>&1; then ok=1; break; fi
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
done
for page in / /climate-admin /static/home.js /api/climate/zones; do
    [ "$ok" = 1 ] && curl -fsS -o /dev/null "http://127.0.0.1:$PORT$page" || { ok=0; echo "   $page: no answer"; }
done
kill "$pid" 2>/dev/null || true
wait "$pid" 2>/dev/null || true
if [ "$ok" != 1 ]; then
    echo "   FAILED - the program's log:"
    tail -n 60 "$SMOKE/log"
    exit 1
fi
echo "   ok"

echo "== Package"
/venv/bin/python /src/packaging/stage.py "$DIST" /src "$WORK/stage" "$VERSION"
DEB=/out/localhome_${VERSION}_arm64.deb
dpkg-deb --root-owner-group -Zxz --build "$WORK/stage" "$DEB"
echo "   $DEB ($(du -h "$DEB" | cut -f1))"
