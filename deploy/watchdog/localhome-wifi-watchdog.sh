#!/bin/sh
# LocalHome's Wi-Fi watchdog: the board is on Wi-Fi around the clock and
# nobody is there to notice when it isn't. Run every minute by
# localhome-wifi-watchdog.timer, as root - independent of LocalHome itself,
# so it works even when LocalHome doesn't. Installed by deploy/tune-board.sh.
#
# Every run:
#   - the Wi-Fi radio is on, and every Wi-Fi profile retries for ever with
#     power saving off (a profile added later - a new access point - gets
#     the same, the first minute it exists). Any band: the board joins
#     whichever the router offers, like any other device - pinning it to
#     2.4 GHz once cut the Shellys off on a router that wouldn't pass
#     connections between two devices on the same radio;
#   - once per boot, two minutes in, the Wi-Fi is reconnected anyway: on
#     both test boards a fresh boot sometimes left the router passing
#     pings to the board but not connections (web page, SSH, MQTT) - a
#     state no check from the board can see, cleared by a reconnect every
#     time. A few seconds of Wi-Fi; the Shellys reconnect by themselves;
#   - the router (the default gateway) is pinged. Not answering, it steps
#     up: 3 min -> reconnect the Wi-Fi, 10 min -> restart NetworkManager,
#     20 min -> reconnect again, 30 min -> reboot the board (not in its
#     first 30 min of uptime, at most once every 6 hours: a router that's
#     switched off doesn't make the board reboot all night);
#   - the router answering, the devices that stay connected to the board's
#     broker are counted - the mains Shellys, an H&T on USB-C: some were,
#     none are for 10 min -> reconnect the Wi-Fi, again at 30. The router
#     has stopped passing their connections (LocalHome shows RT and asks
#     for the router to be restarted). A battery sensor, connected for a
#     few seconds when it wakes, doesn't count.
#
# What it does goes to the journal and, being rare, to a small log on disk
# that survives the reboot it may cause: one line per action, a date, a
# code and its values ("2026-09-27 03:12:40 reconnect 3 wlan0") - read by
# LocalHome's admin panel, which words each code in the page's language
# (system status -> watchdog history, and the diagnostics download).
#
# It can't see everything: a router that blocks phones and PCs but still
# passes the Shellys looks fine from here. A fixed IP (a DHCP reservation)
# for the board and 5 GHz on a non-radar channel (36-48) help.

# Where it keeps its state and its log - other places only for trying it
# out (tests/test_wifi_watchdog.py), like the uptime below.
STATE=${LOCALHOME_WIFI_WATCHDOG_STATE:-/run/localhome-wifi-watchdog}
DATA=${LOCALHOME_WIFI_WATCHDOG_DATA:-/var/lib/localhome}
LOG=$DATA/wifi-watchdog.log
LAST_REBOOT=$DATA/wifi-watchdog-reboot

mkdir -p "$STATE" "$DATA"

# log <code> [values...] - codes: radio_on, profile_fixed <name>, boot_reconnect,
# router_down <gateway>, back <seconds>, reconnect <minutes> <device>,
# nm_restart <minutes>, reboot <minutes>, reboot_skipped <minutes>,
# lan_silent <minutes>, lan_back <seconds>.
log() {
    echo "$*"
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"
    tail -n 200 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
    chmod 644 "$LOG"
}

uptime_s=${LOCALHOME_WIFI_WATCHDOG_UPTIME:-$(cut -d. -f1 /proc/uptime)}

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

    # -------------------------- the router answers: do the devices? ----
    # Mains-powered Shellys stay connected to the board's MQTT broker all
    # the time. Some were, none are, and the router still answers: the
    # router has stopped passing their connections to the board (seen on
    # the production board: it passed pings, not connections, between two
    # devices on the same radio, until it was restarted). Re-activating
    # the Wi-Fi is all the board can do - at 10 and 30 minutes; LocalHome
    # shows it as "RT" and asks for the router to be restarted.
    # A device that stays is a connection - its address and port - already
    # there at the last check, a minute ago: a mains Shelly, an H&T on
    # USB-C. A battery sensor connects for a few seconds when it wakes and
    # goes back to sleep: caught by one check, it made a working router
    # look broken 10 minutes later - the Wi-Fi reconnected twice every 2
    # hours (seen on a board with only its H&T connected).
    peers=$(ss -Htn state established '( sport = :1883 )' 2>/dev/null \
        | awk '$4 !~ /^(127\.|\[::1\]|\[::ffff:127\.)/ {print $4}' | sort -u)
    lan=0
    if [ -n "$peers" ] && [ -s "$STATE/lan_peers" ]; then
        lan=$(printf '%s\n' "$peers" | grep -cxFf "$STATE/lan_peers" || true)
    fi
    if [ -n "$peers" ]; then
        printf '%s\n' "$peers" > "$STATE/lan_peers"
    else
        rm -f "$STATE/lan_peers"
    fi
    if [ "$lan" -gt 0 ]; then
        touch "$STATE/lan_seen"
        if [ -f "$STATE/lan_silent_since" ]; then
            log lan_back "$((uptime_s - $(cat "$STATE/lan_silent_since")))"
            rm -f "$STATE/lan_silent_since" "$STATE/lan_reconnect1" "$STATE/lan_reconnect2"
        fi
    elif [ -f "$STATE/lan_seen" ]; then
        [ -f "$STATE/lan_silent_since" ] || echo "$uptime_s" > "$STATE/lan_silent_since"
        silent=$((uptime_s - $(cat "$STATE/lan_silent_since")))
        for step in "1800 lan_reconnect2" "600 lan_reconnect1"; do
            limit=${step% *}
            flag=$STATE/${step#* }
            if [ "$silent" -ge "$limit" ] && [ ! -f "$flag" ]; then
                touch "$STATE/lan_reconnect1" "$flag"
                log lan_silent "$((silent / 60))"
                wifi_up
                break
            fi
        done
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
