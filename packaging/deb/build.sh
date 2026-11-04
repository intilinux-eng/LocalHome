#!/bin/sh
# Builds localhome_<version>_<arch>.deb inside the container (Dockerfile):
#
#   /in/src.tar  LocalHome's files (tools/build_deb.py)
#   /out         where the package is written
#
#   build-localhome-deb VERSION [--test]
#
# LocalHome goes into its own Python environment in /opt/localhome, with every
# module but the packages' __init__ compiled by Cython and its .py removed:
# the package carries no source of LocalHome itself. Its pages, scripts and
# texts stay as they are - a browser has to read them. --test then runs the
# unit tests against the compiled modules, before anything is packaged.
set -eu
VERSION=$1
TEST=${2:-}
APP=/opt/localhome
STAGE=/tmp/stage
SRC=/tmp/src
DEB=$SRC/packaging/deb
ARCH=$(dpkg --print-architecture)

rm -rf "$SRC" && mkdir -p "$SRC" && tar -xf /in/src.tar -C "$SRC"
# Files from a Windows checkout come writable by anyone: on the board some
# run as root (tune-board.sh). Read for all, written by their owner only.
chmod -R u+rwX,go+rX,go-w "$SRC"
find "$SRC" -name "*.sh" -exec chmod 755 {} +
# What runs on the board as shell or systemd reads it: Linux line endings,
# whatever the PC's checkout has.
find "$SRC/deploy" "$SRC/packaging" -type f ! -name '*.so' -exec sed -i 's/\r$//' {} +
sed -i 's/\r$//' "$SRC/config/config.example.yaml" "$SRC/config/mqtt_broker.example.json"

echo "== LocalHome $VERSION for $ARCH"
rm -rf "$APP" "$STAGE"
python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade pip
"$APP/venv/bin/pip" install -q -c "$SRC/constraints.txt" "$SRC"

SITE=$("$APP/venv/bin/python" -c "import sysconfig; print(sysconfig.get_paths()['purelib'])")
PKG="$SITE/localhome"
# The pages, scripts and texts as the source has them, all of them -
# pyproject.toml's package-data leaves out static/home/.
for folder in static templates locales; do
    rm -rf "$PKG/web/$folder"
    cp -a "$SRC/src/localhome/web/$folder" "$PKG/web/$folder"
done
find "$PKG" -name __pycache__ -prune -exec rm -rf {} +

echo "== compiling $(find "$PKG" -name '*.py' ! -name __init__.py | wc -l) modules"
# annotation_typing off: a type hint stays a hint, as in Python - with it on,
# Cython would refuse a float where a hint says int.
cd "$PKG/.."
find localhome -name '*.py' ! -name __init__.py > /tmp/modules
# Two steps: Cython makes each module C, gcc compiles each C file by itself -
# so a module Cython can't take (a Python form it doesn't know) doesn't stop
# the others; it goes as bytecode instead, still without its source.
xargs -P "$(nproc)" -n 1 /tools/bin/cythonize -q \
    --directive language_level=3,annotation_typing=False,binding=True,embedsignature=False \
    < /tmp/modules >/tmp/cythonize.log 2>&1 || true
INCLUDE=$(python3 -c "import sysconfig; print(sysconfig.get_paths()['include'])")
SUFFIX=$(python3 -c "import sysconfig; print(sysconfig.get_config_var('EXT_SUFFIX'))")
sed 's/\.py$//' /tmp/modules | xargs -P "$(nproc)" -I{} sh -c \
    '[ -f "{}.c" ] && gcc -shared -fPIC -O2 -g0 -DNDEBUG -fno-strict-aliasing -I"$0" -o "{}$1" "{}.c" 2>>/tmp/gcc.log || true' \
    "$INCLUDE" "$SUFFIX"
: > /tmp/bytecode
while read -r module; do
    if ! ls "${module%.py}".*.so >/dev/null 2>&1; then
        "$APP/venv/bin/python" -c "import py_compile, sys; py_compile.compile(sys.argv[1], sys.argv[1] + 'c', doraise=True)" "$module"
        echo "$module" >> /tmp/bytecode
    fi
done < /tmp/modules
if [ -s /tmp/bytecode ]; then
    echo "== as bytecode, Cython couldn't compile them:"
    sed 's/^/   /' /tmp/bytecode
    grep -E "\.py:[0-9]+:[0-9]+: " /tmp/cythonize.log | sort -u | sed 's/^/   /' || true
    [ -s /tmp/gcc.log ] && grep -m5 "error" /tmp/gcc.log | sed 's/^/   gcc: /'
fi
xargs rm -f < /tmp/modules
find localhome -name '*.c' -delete
rm -rf build
cd /

# The libraries' own bytecode, made now: /opt is read-only to the service.
"$APP/venv/bin/python" -m compileall -q "$SITE" >/dev/null || true
"$APP/venv/bin/python" - <<EOF
import localhome, localhome.web.app, localhome.config
assert localhome.config.__file__.endswith(".so"), localhome.config.__file__
print("== imports: localhome", localhome.__version__, "compiled")
EOF

if [ "$TEST" = "--test" ]; then
    echo "== unit tests against the compiled modules"
    "$APP/venv/bin/pip" install -q -c "$SRC/constraints.txt" pytest pytest-xdist
    # The tests read the repository's files (pages, scripts) but import the
    # installed, compiled package: src/ isn't on the path - checked first.
    cat > "$SRC/tests/test_zz_compiled.py" <<'PY'
def test_the_modules_tested_are_the_compiled_ones():
    import localhome.config
    import localhome.web.app
    assert localhome.config.__file__.endswith(".so") and localhome.web.app.__file__.endswith(".so")
PY
    # test_deploy_board tests the PC's deploy tool, with git - not the board's code.
    cd "$SRC" && "$APP/venv/bin/python" -m pytest -n "$(nproc)" -q -p no:cacheprovider         --ignore=tests/test_deploy_board.py || exit 1
    cd /
    "$APP/venv/bin/pip" uninstall -y -q pytest pytest-xdist execnet iniconfig pluggy packaging >/dev/null 2>&1 || true
fi

echo "== packaging"
mkdir -p "$STAGE/DEBIAN" "$STAGE/opt" "$STAGE/usr/lib/systemd/system" "$STAGE/usr/lib/localhome" \
         "$STAGE/usr/share/localhome" "$STAGE/etc/mosquitto/conf.d" "$STAGE/etc/default"
cp -a "$APP" "$STAGE/opt/"
cp "$DEB/localhome.service" "$STAGE/usr/lib/systemd/system/"
cp -a "$SRC/deploy" "$STAGE/usr/lib/localhome/deploy"
rm -f "$STAGE/usr/lib/localhome/deploy/localhome.service"
cp "$SRC/config/config.example.yaml" "$SRC/config/mqtt_broker.example.json" "$STAGE/usr/share/localhome/"
cp "$DEB/mosquitto-localhome.conf" "$STAGE/etc/mosquitto/conf.d/localhome.conf"
cp "$DEB/default" "$STAGE/etc/default/localhome"
for script in postinst prerm postrm; do install -m 755 "$DEB/$script" "$STAGE/DEBIAN/"; done
printf '/etc/mosquitto/conf.d/localhome.conf\n/etc/default/localhome\n' > "$STAGE/DEBIAN/conffiles"
chmod -R go-w "$STAGE"
SIZE=$(du -sk "$STAGE" | cut -f1)
sed -e "s/@VERSION@/$VERSION/" -e "s/@ARCH@/$ARCH/" -e "s/@SIZE@/$SIZE/" "$DEB/control.in" > "$STAGE/DEBIAN/control"
find "$STAGE" -name __pycache__ -path "*/deploy/*" -prune -exec rm -rf {} +
dpkg-deb --root-owner-group -Zxz --build "$STAGE" "/out/localhome_${VERSION}_${ARCH}.deb" >/dev/null
echo "== /out/localhome_${VERSION}_${ARCH}.deb: $(du -h "/out/localhome_${VERSION}_${ARCH}.deb" | cut -f1)"
if find "$STAGE/opt/localhome/venv/lib" -path '*/site-packages/localhome/*.py' ! -name __init__.py | grep -q .; then
    echo "a LocalHome source file is in the package" >&2
    exit 1
fi
