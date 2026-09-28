#!/bin/sh
# One-time tuning for a board that runs LocalHome unattended on eMMC/SD
# storage - confirmed on an Arduino UNO Q, plain Debian otherwise. Cuts
# the storage writes that wear the flash out (measured on a real UNO Q:
# ~7.5 GB/day before, ~0.6 GB/day after), stops what the board doesn't
# need, and makes it look after itself on Wi-Fi around the clock: Wi-Fi
# never dozing or giving up, a watchdog that reconnects it, and restarts
# for LocalHome or the whole board if they hang. Safe to run again - and
# worth running again after a LocalHome update; every step prints how to
# undo it.
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
HERE=$(cd "$(dirname "$0")" && pwd)
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

echo "== 5. Wi-Fi: never dozing, never giving up"
# The board lives on Wi-Fi 24/7. Power saving lets the radio sleep between
# beacons - missed connections from phones and Shellys; and after a few
# failed attempts NetworkManager stops trying a network. Both off, for
# every Wi-Fi profile - and for any added later, by the watchdog in step 6.
# And 2.4 GHz only, where the network is there on 2.4 GHz: steadier with
# most routers, no radar channels that throw every device off, and the
# Shellys' band anyway. (touch /etc/localhome/wifi-any-band beforehand to
# leave the band alone; the watchdog frees it by itself if the board
# can't connect for 20 minutes.) The band applies from the next
# connection: reboot once at the end.
if command -v nmcli >/dev/null 2>&1; then
    mkdir -p /etc/NetworkManager/conf.d
    cat > /etc/NetworkManager/conf.d/99-localhome-wifi.conf <<'EOF'
# LocalHome board: Wi-Fi power saving off for every connection - see
# deploy/tune-board.sh. (Retrying for ever is per profile: step 6's
# watchdog sets it on each one, new ones included.)
[connection]
wifi.powersave=2
EOF
    nmcli general reload conf
    nmcli radio wifi on
    nmcli -t -f NAME,TYPE connection show | while IFS= read -r line; do
        [ "${line##*:}" = "802-11-wireless" ] || continue
        name=$(printf '%s' "${line%:*}" | sed 's/\\:/:/g')
        nmcli connection modify "$name" connection.autoconnect yes connection.autoconnect-retries 0 \
            802-11-wireless.powersave 2
        echo "   profile '$name': reconnects for ever, power saving off (from its next connection)"
        ssid=$(nmcli -g 802-11-wireless.ssid connection show "$name")
        if [ -f /etc/localhome/wifi-any-band ]; then
            echo "   profile '$name': band left alone (/etc/localhome/wifi-any-band)"
        elif nmcli -t -f FREQ,SSID device wifi list 2>/dev/null | sed 's/\\:/:/g' \
                | awk -v s="$ssid" '$0+0 < 3000 && substr($0, index($0, ":") + 1) == s {found = 1} END {exit !found}'; then
            nmcli connection modify "$name" 802-11-wireless.band bg
            echo "   profile '$name': 2.4 GHz only (from its next connection)"
        else
            echo "   profile '$name': not seen on 2.4 GHz here - band left alone"
        fi
    done
    echo "   undo: rm /etc/NetworkManager/conf.d/99-localhome-wifi.conf; nmcli connection modify <name> connection.autoconnect-retries -1 802-11-wireless.powersave 0 802-11-wireless.band ''"
else
    echo "   no NetworkManager here - nothing to do"
fi

echo "== 6. Wi-Fi watchdog"
# Every minute, independent of LocalHome: the router not answering for
# 3 min -> reconnect, 10 min -> restart NetworkManager, 30 min -> reboot
# (at most every 6 h). See deploy/watchdog/localhome-wifi-watchdog.sh.
if command -v nmcli >/dev/null 2>&1; then
    install -D -m 755 "$HERE/watchdog/localhome-wifi-watchdog.sh" /usr/local/lib/localhome/localhome-wifi-watchdog.sh
    install -m 644 "$HERE/watchdog/localhome-wifi-watchdog.service" "$HERE/watchdog/localhome-wifi-watchdog.timer" \
        /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable --now localhome-wifi-watchdog.timer
    echo "   what it did: cat /var/lib/localhome/wifi-watchdog.log (also in the admin panel)"
    echo "   undo: systemctl disable --now localhome-wifi-watchdog.timer"
else
    echo "   no NetworkManager here - nothing to do"
fi

echo "== 7. Hardware watchdog: a frozen board reboots by itself"
# systemd feeds the chip's watchdog; if the whole system hangs - kernel,
# systemd - the chip resets the board. Off on a stock UNO Q image.
if [ -e /dev/watchdog ]; then
    max=$(cat /sys/class/watchdog/watchdog0/max_timeout 2>/dev/null || echo 30)
    seconds=$(( max < 30 ? max : 30 ))
    mkdir -p /etc/systemd/system.conf.d
    cat > /etc/systemd/system.conf.d/localhome-watchdog.conf <<EOF
# LocalHome board: a hung system is reset by the hardware watchdog - see
# deploy/tune-board.sh.
[Manager]
RuntimeWatchdogSec=${seconds}s
RebootWatchdogSec=10min
EOF
    systemctl daemon-reexec
    echo "   on: ${seconds}s"
    echo "   undo: rm /etc/systemd/system.conf.d/localhome-watchdog.conf && systemctl daemon-reexec"
else
    echo "   no /dev/watchdog on this board - nothing to do"
fi

echo "== 8. LocalHome restarted if it hangs"
# LocalHome tells systemd it's alive while its web page answers and its
# control loops run (see services/service_watchdog.py); two minutes of
# silence and systemd restarts it - a hang, not only a crash.
UNIT=/etc/systemd/system/localhome.service
if [ -f "$UNIT" ] && grep -q "WATCHDOG=1" "$HERE/../src/localhome/services/service_watchdog.py" 2>/dev/null; then
    install -D -m 755 "$HERE/watchdog/localhome-service-stopped.sh" /usr/local/lib/localhome/localhome-service-stopped.sh
    mkdir -p /etc/systemd/system/localhome.service.d
    cat > /etc/systemd/system/localhome.service.d/localhome-watchdog.conf <<'EOF'
# LocalHome board: restarted when it stops saying it's alive - see
# deploy/tune-board.sh and src/localhome/services/service_watchdog.py.
# The stop hook notes a watchdog restart in the admin panel's history.
[Unit]
StartLimitIntervalSec=0

[Service]
ExecStopPost=+/bin/sh /usr/local/lib/localhome/localhome-service-stopped.sh
; Short of memory, the kernel sacrifices anything else first.
OOMScoreAdjust=-500
Type=notify
NotifyAccess=main
WatchdogSec=120
TimeoutStartSec=180
Restart=always
RestartSec=5
EOF
    systemctl daemon-reload
    systemctl restart localhome
    echo "   on: restarted after 2 min without a sign of life"
    echo "   undo: rm -r /etc/systemd/system/localhome.service.d && systemctl daemon-reload && systemctl restart localhome"
elif [ -f "$UNIT" ]; then
    echo "   this LocalHome is too old to say it's alive - update it, then run this again"
else
    echo "   no localhome.service installed - nothing to do"
fi

echo "== 9. No power saving anywhere"
# The board is plugged in, all year: working beats saving power. It can
# never go to sleep; Bluetooth - unused, and sharing the radio with the
# Wi-Fi on this chip - is off; USB devices and the eMMC controller never
# doze. (The CPU still changes speed as needed: it's instant, and always
# at full speed would only heat a closed cabinet.)
systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target >/dev/null 2>&1
if systemctl list-unit-files bluetooth.service >/dev/null 2>&1; then
    systemctl disable --now bluetooth.service >/dev/null 2>&1 || true
fi
command -v rfkill >/dev/null 2>&1 && rfkill block bluetooth 2>/dev/null || true
cat > /etc/tmpfiles.d/localhome-power.conf <<'EOF'
# LocalHome board: no power saving on USB or the eMMC - see deploy/tune-board.sh.
w /sys/module/usbcore/parameters/autosuspend - - - - -1
w /sys/bus/usb/devices/*/power/control - - - - on
w /sys/bus/mmc/devices/*/power/control - - - - on
w /sys/class/mmc_host/*/device/power/control - - - - on
EOF
systemd-tmpfiles --create /etc/tmpfiles.d/localhome-power.conf 2>/dev/null || true
echo "   on: no sleep, Bluetooth off, USB and eMMC always powered"
echo "   undo: systemctl unmask sleep.target suspend.target hibernate.target hybrid-sleep.target; rfkill unblock bluetooth; systemctl enable --now bluetooth; rm /etc/tmpfiles.d/localhome-power.conf"

echo "== 10. The essential services always come back"
# LocalHome is only as good as the broker the Shellys talk to, the name
# service that makes <name>.local work and the network manager: each is
# restarted whenever it stops, however it stopped, and never given up on.
# Short of memory, the broker is sacrificed after almost everything else.
for unit in mosquitto avahi-daemon NetworkManager; do
    if systemctl list-unit-files "$unit.service" >/dev/null 2>&1; then
        mkdir -p "/etc/systemd/system/$unit.service.d"
        {
            echo "# LocalHome board: always restarted - see deploy/tune-board.sh."
            echo "[Unit]"
            echo "StartLimitIntervalSec=0"
            echo "[Service]"
            echo "Restart=always"
            echo "RestartSec=5"
            [ "$unit" = mosquitto ] && echo "OOMScoreAdjust=-300"
        } > "/etc/systemd/system/$unit.service.d/localhome-restart.conf"
        echo "   $unit: always restarted"
    fi
done
systemctl daemon-reload
echo "   undo: rm /etc/systemd/system/{mosquitto,avahi-daemon,NetworkManager}.service.d/localhome-restart.conf && systemctl daemon-reload"

if [ "$LEAN" = 1 ]; then
    echo "== 11. Lean: remove what LocalHome doesn't use"
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
for unit in localhome mosquitto avahi-daemon localhome-wifi-watchdog.timer; do
    echo "   $unit: $(systemctl is-active "$unit" 2>/dev/null || true)"
done
echo "   hardware watchdog: $(systemctl show -p RuntimeWatchdogUSec --value 2>/dev/null)"
echo "   sleep: $(systemctl is-enabled sleep.target 2>/dev/null)   bluetooth: $(systemctl is-active bluetooth 2>/dev/null || true)"
echo "   Reboot once now (sudo systemctl reboot): the Wi-Fi band and the watchdogs take effect from the next boot."
