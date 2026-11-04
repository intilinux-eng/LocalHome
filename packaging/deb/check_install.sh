#!/bin/sh
# The package on a clean Debian, as a board gets it: installed with apt (its
# dependencies come with it), LocalHome started as its service starts it, its
# pages and API asked, installed again over itself, removed - its data kept.
# There's no systemd in a container: the service's own command is run by hand.
#
#   check_install.sh /out/localhome_<version>_<arch>.deb
set -eu
DEB=$1
fail() { echo "FAIL: $*" >&2; tail -40 /tmp/localhome.log 2>/dev/null >&2; exit 1; }

apt-get update -qq
apt-get install -y -qq curl >/dev/null
echo "== apt install $(basename "$DEB")"
apt-get install -y -qq "$DEB" >/tmp/apt.log 2>&1 || { tail -30 /tmp/apt.log; fail "apt install"; }
id arduino >/dev/null || fail "no arduino user"
[ -f /var/lib/localhome/config.yaml ] || fail "no first configuration"
grep -q '"localhost"' /var/lib/localhome/mqtt_broker.json || fail "no broker settings for LocalHome"
runuser -u arduino -- env LOCALHOME_CONFIG=/var/lib/localhome/config.yaml /opt/localhome/venv/bin/localhome-cli doctor \
    || fail "localhome-cli doctor"
[ "$(stat -c %U /var/lib/localhome)" = arduino ] || fail "data not owned by arduino"
grep -q "allow_anonymous true" /etc/mosquitto/conf.d/localhome.conf || fail "no broker configuration"
writable=$(find /opt/localhome /usr/lib/localhome /usr/share/localhome /usr/lib/systemd/system/localhome.service \
    -perm -o+w ! -type l 2>/dev/null | head -3)
[ -z "$writable" ] || fail "writable by anyone: $writable"
if find /opt/localhome/venv/lib -path '*/site-packages/localhome/*.py' ! -name __init__.py | grep -q .; then
    fail "LocalHome source in the package"
fi

start() {
    cd /var/lib/localhome
    runuser -u arduino -- env LOCALHOME_CONFIG=/var/lib/localhome/config.yaml /opt/localhome/venv/bin/localhome \
        >/tmp/localhome.log 2>&1 &
    echo $! > /tmp/localhome.pid
    i=0
    until curl -sf -o /dev/null http://127.0.0.1:5000/api/server-time; do
        i=$((i + 1))
        [ $i -lt 180 ] || fail "LocalHome didn't answer in 3 minutes"
        kill -0 "$(cat /tmp/localhome.pid)" 2>/dev/null || fail "LocalHome stopped"
        sleep 1
    done
    echo "   answering after ${i} s"
}

stop() {
    kill "$(cat /tmp/localhome.pid)" 2>/dev/null || true
    sleep 2
}

echo "== started as its service starts it"
start
for path in / /climate-admin /api/server-time /api/climate/zones /api/devices /api/problems /api/system-status \
            /static/home/core.js /static/home/zones.js /static/guide/shelly-plus1-valvole.html /site.webmanifest; do
    code=$(curl -s -o /tmp/body -w '%{http_code}' "http://127.0.0.1:5000$path")
    [ "$code" = 200 ] || fail "$path answered $code"
    echo "   $path $code $(wc -c < /tmp/body) bytes"
done
grep -q "Traceback" /tmp/localhome.log && fail "a Python error in the log"
stop

echo "== installed again over itself (an update)"
echo "# kept" >> /var/lib/localhome/config.yaml
apt-get install -y -qq --reinstall "$DEB" >/tmp/apt.log 2>&1 || { tail -30 /tmp/apt.log; fail "reinstall"; }
grep -q "# kept" /var/lib/localhome/config.yaml || fail "the configuration was replaced"
start
stop

echo "== removed"
apt-get purge -y -qq localhome >/tmp/apt.log 2>&1 || { tail -30 /tmp/apt.log; fail "purge"; }
[ ! -e /opt/localhome ] || fail "/opt/localhome left behind"
[ -f /var/lib/localhome/config.yaml ] || fail "the data went with the package"
[ -d /var/lib/localhome/data ] && echo "   data kept: $(ls /var/lib/localhome | tr '\n' ' ')"
echo "== all good"
