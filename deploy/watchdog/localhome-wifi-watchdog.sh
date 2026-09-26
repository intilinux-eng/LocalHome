#!/bin/sh
# LocalHome's Wi-Fi watchdog: the board is on Wi-Fi around the clock and
# nobody is there to notice when it isn't. Run every minute by
# localhome-wifi-watchdog.timer, as root - independent of LocalHome itself,
# so it works even when LocalHome doesn't. Installed by deploy/tune-board.sh.
#
# Every run:
#   - the Wi-Fi radio is on, and every Wi-Fi profile retries for ever with
#     power saving off (a profile added later - a new access point - gets
#     the same, the first minute it exists);
#   - once per boot, two minutes in, the Wi-Fi is reconnected anyway: on
#     both test boards a fresh boot sometimes left the router passing
#     pings to the board but not connections (web page, SSH, MQTT) - a
#     state no check from the board can see, cleared by a reconnect every
#     time. A few seconds of Wi-Fi; the Shellys reconnect by themselves;
#   - the router (the default gateway) is pinged. Not answering, it steps
#     up: 3 min -> reconnect the Wi-Fi, 10 min -> restart NetworkManager,
#     20 min -> reconnect again, 30 min -> reboot the board (not in its
#     first 30 min of uptime, at most once every 6 hours: a router that's
#     switched off doesn't make the board reboot all night).
#
# What it does goes to the journal and, being rare, to a small log on disk
# that survives the reboot it may cause: one line per action, a date, a
# code and its values ("2026-09-27 03:12:40 reconnect 3 wlan0") - read by
# LocalHome's admin panel, which words each code in the page's language
# (system status -> watchdog history, and the diagnostics download).
#
# It can't see everything: today's router answering while phones and PCs
# can't reach the board is invisible from here - a fixed IP (a DHCP
# reservation) for the board in the router is still worth having.

STATE=/run/localhome-wifi-watchdog
DATA=/var/lib/localhome
LOG=$DATA/wifi-watchdog.log
LAST_REBOOT=$DATA/wifi-watchdog-reboot

mkdir -p "$STATE" "$DATA"

# log <code> [values...] - codes: radio_on, profile_fixed <name>, boot_reconnect,
# router_down <gateway>, back <seconds>, reconnect <minutes> <device>,
# nm_restart <minutes>, reboot <minutes>, reboot_skipped <minutes>.
log() {
    echo "$*"
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"
    tail -n 200 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
    chmod 644 "$LOG"
}

uptime_s=$(cut -d. -f1 /proc/uptime)

# --------------------------------------------------- Wi-Fi never off ----
if command -v nmcli >/dev/null 2>&1; then
    if [ "$(nmcli radio wifi 2>/dev/null)" = "disabled" ]; then
        nmcli radio wifi on && log radio_on
    fi
    nmcli -t -f NAME,TYPE connection show 2>/dev/null | while IFS= read -r line; do
        type=${line##*:}
        name=${line%:*}
        name=$(printf '%s' "$name" | sed 's/\\:/:/g')
        [ "$type" = "802-11-wireless" ] || continue
        retries=$(nmcli -g connection.autoconnect-retries connection show "$name" 2>/dev/null)
        powersave=$(nmcli -g 802-11-wireless.powersave connection show "$name" 2>/dev/null)
        auto=$(nmcli -g connection.autoconnect connection show "$name" 2>/dev/null)
        if [ "$retries" != "0" ] || [ "$auto" != "yes" ] || { [ "$powersave" != "2" ] && [ "$powersave" != "disable" ]; }; then
            nmcli connection modify "$name" connection.autoconnect yes connection.autoconnect-retries 0 \
                802-11-wireless.powersave 2 \
                && log profile_fixed "$name"
        fi
    done
fi

# ------------------------------------------------ is the router there ----
gateway=$(ip route show default 2>/dev/null | awk '{print $3; exit}')
# For trying the watchdog out without unplugging the router:
# LOCALHOME_WIFI_WATCHDOG_TARGET=<an address that doesn't answer>.
gateway=${LOCALHOME_WIFI_WATCHDOG_TARGET:-$gateway}
wifi_dev=$(nmcli -t -f DEVICE,TYPE device 2>/dev/null | awk -F: '$2=="wifi" {print $1; exit}')

# Re-activating the Wi-Fi profile is what clears a stuck connection (seen
# on both test boards); `nmcli device reconnect` doesn't exist, and
# `device connect` on a connected device may do nothing.
wifi_up() {
    profile=$(nmcli -g GENERAL.CONNECTION device show "$wifi_dev" 2>/dev/null | sed 's/\\:/:/g')
    if [ -n "$profile" ]; then
        nmcli connection up "$profile" >/dev/null 2>&1
    else
        nmcli device connect "$wifi_dev" >/dev/null 2>&1
    fi
}

if [ -n "$wifi_dev" ] && [ ! -f "$STATE/boot_reconnect_done" ] && [ "$uptime_s" -ge 90 ]; then
    touch "$STATE/boot_reconnect_done"
    log boot_reconnect
    wifi_up
    sleep 5
fi

up=0
if [ -n "$gateway" ]; then
    for try in 1 2 3; do
        if ping -c 1 -W 2 "$gateway" >/dev/null 2>&1; then up=1; break; fi
        sleep 2
    done
fi

if [ "$up" = 1 ]; then
    if [ -f "$STATE/down_since" ]; then
        log back "$((uptime_s - $(cat "$STATE/down_since")))"
        rm -f "$STATE/down_since" "$STATE/reconnect1" "$STATE/reconnect2" "$STATE/nm_restart" \
            "$STATE/reboot_skipped"
    fi
    exit 0
fi

if [ ! -f "$STATE/down_since" ]; then
    echo "$uptime_s" > "$STATE/down_since"
    log router_down "${gateway:-none}"
fi
down=$((uptime_s - $(cat "$STATE/down_since")))

# Each step once per outage.
first_time() {
    [ -f "$STATE/$1" ] && return 1
    touch "$STATE/$1"
}

reconnect() {
    log reconnect "$((down / 60))" "$wifi_dev"
    wifi_up
}

if [ "$down" -ge 1800 ]; then
    now=$(date +%s)
    last=$(cat "$LAST_REBOOT" 2>/dev/null || echo 0)
    if [ "$uptime_s" -ge 1800 ] && [ $((now - last)) -ge 21600 ]; then
        log reboot "$((down / 60))"
        echo "$now" > "$LAST_REBOOT"
        sync
        systemctl reboot
    elif first_time reboot_skipped; then
        log reboot_skipped "$((down / 60))"
    fi
elif [ "$down" -ge 1200 ] && first_time reconnect2; then
    reconnect
elif [ "$down" -ge 600 ] && first_time nm_restart; then
    log nm_restart "$((down / 60))"
    systemctl restart NetworkManager
elif [ "$down" -ge 180 ] && first_time reconnect1; then
    reconnect
fi
exit 0
