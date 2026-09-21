#!/bin/sh
# One-time tuning for a board that runs LocalHome unattended on eMMC/SD
# storage - confirmed on an Arduino UNO Q, plain Debian otherwise. Cuts
# the storage writes that wear the flash out (measured on a real UNO Q:
# ~7.5 GB/day before, ~0.6 GB/day after) and stops what the board doesn't
# need. Safe to run again; every step prints how to undo it.
#
#   sudo sh deploy/tune-board.sh
#
# See docs/deployment.md#tune-the-board-for-long-unattended-running.
set -e

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

echo "== 3. No graphical desktop (no monitor attached)"
if systemctl list-unit-files lightdm.service >/dev/null 2>&1; then
    systemctl set-default multi-user.target
    systemctl disable --now lightdm 2>/dev/null || true
    echo "   undo: systemctl set-default graphical.target && systemctl enable --now lightdm"
else
    echo "   no lightdm here - nothing to do"
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
