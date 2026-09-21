#!/bin/sh
# One-time tuning for a board that runs LocalHome unattended on eMMC/SD
# storage - confirmed on an Arduino UNO Q, plain Debian otherwise. Cuts
# the storage writes that wear the flash out (measured on a real UNO Q:
# ~7.5 GB/day before, ~0.6 GB/day after) and stops what the board doesn't
# need. Safe to run again; every step prints how to undo it.
#
#   sudo sh deploy/tune-board.sh           # the essentials, for every board
#   sudo sh deploy/tune-board.sh --lean    # also remove what LocalHome doesn't use
#
# --lean additionally turns off Arduino App Lab and its Docker runtime,
# the firmware updater (fwupd), removes App Lab's unused Docker images and
# empties the package cache: ~270 MB of RAM and ~2.3 GB of disk back,
# which matters most on the 2 GB / 16 GB UNO Q. App Lab can't be used on
# the board until they're turned back on (the undo line is printed). The
# USB rescue access (adbd) and the Linux-MCU bridge (arduino-router) stay.
#
# See docs/arduino-uno-q.md and docs/deployment.md#tune-the-board-for-long-unattended-running.
set -e
LEAN=0
[ "$1" = "--lean" ] && LEAN=1

if [ "$(id -u)" -ne 0 ]; then
    echo "Run it with sudo: sudo sh deploy/tune-board.sh" >&2
    exit 1
fi

echo "== 1. System log in RAM only"
# journald's own write amplification (several 4 KB pages per log line)
# was most of the board's flash writes - ~5 GB/day. In RAM, capped at
# 30 MB; the log is lost at reboot (LocalHome's admin panel keeps
# showing the system status regardless).
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/localhome-volatile.conf <<'EOF'
# LocalHome board: keep the journal in RAM only - see deploy/tune-board.sh.
[Journal]
Storage=volatile
RuntimeMaxUse=30M
EOF
systemctl restart systemd-journald
echo "   undo: rm /etc/systemd/journald.conf.d/localhome-volatile.conf && systemctl restart systemd-journald"

echo "== 2. No background update downloads"
# Nothing installs by itself on a stock image (no unattended-upgrades),
# but these download package/firmware lists every day. Update by hand
# when you choose: sudo apt update && sudo apt upgrade
for timer in apt-daily.timer apt-daily-upgrade.timer fwupd-refresh.timer; do
    if systemctl list-unit-files "$timer" >/dev/null 2>&1; then
        systemctl disable --now "$timer" 2>/dev/null || true
    fi
done
echo "   undo: systemctl enable --now apt-daily.timer apt-daily-upgrade.timer fwupd-refresh.timer"

echo "== 3. MQTT broker log into the journal (RAM)"
# mosquitto ships logging every connection to a file on the eMMC - the
# last log still written there once step 1 is done. Into syslog instead,
# i.e. the journal: same content (journalctl -u mosquitto), no flash writes.
MOSQ=/etc/mosquitto/mosquitto.conf
if [ -f "$MOSQ" ] && grep -q '^log_dest file' "$MOSQ"; then
    sed -i 's|^log_dest file .*|log_dest syslog|' "$MOSQ"
    systemctl restart mosquitto   # devices reconnect by themselves within seconds
    echo "   undo: set 'log_dest file /var/log/mosquitto/mosquitto.log' in $MOSQ and restart mosquitto"
else
    echo "   already done, or no mosquitto here"
fi

echo "== 4. No graphical desktop (no monitor attached)"
if systemctl list-unit-files lightdm.service >/dev/null 2>&1; then
    systemctl set-default multi-user.target
    systemctl disable --now lightdm 2>/dev/null || true
    echo "   undo: systemctl set-default graphical.target && systemctl enable --now lightdm"
else
    echo "   no lightdm here - nothing to do"
fi

if [ "$LEAN" = 1 ]; then
    echo "== 5. Lean: remove what LocalHome doesn't use"
    if command -v docker >/dev/null 2>&1; then
        docker image prune -a -f >/dev/null 2>&1 || true   # App Lab's images, unused by LocalHome
    fi
    for unit in arduino-app-cli.service arduino-cloud-connector.service docker.service docker.socket                 containerd.service fwupd.service; do
        if systemctl list-unit-files "$unit" >/dev/null 2>&1; then
            systemctl disable --now "$unit" >/dev/null 2>&1 || true
        fi
    done
    systemctl mask fwupd.service >/dev/null 2>&1 || true   # otherwise D-Bus starts it again on demand
    apt-get clean
    echo "   undo: systemctl unmask fwupd && systemctl enable --now arduino-app-cli arduino-cloud-connector docker containerd"
fi

echo "== Result"
free -m | sed -n 2p
if [ -d /run/log/journal ] && [ ! -d /var/log/journal/"$(cat /etc/machine-id)" -o -f /etc/systemd/journald.conf.d/localhome-volatile.conf ]; then
    echo "   journal: in RAM (/run/log/journal)"
else
    echo "   journal: still on disk - check journalctl --header"
fi
echo "   default target: $(systemctl get-default)"
for unit in localhome mosquitto avahi-daemon; do
    echo "   $unit: $(systemctl is-active "$unit" 2>/dev/null || true)"
done
