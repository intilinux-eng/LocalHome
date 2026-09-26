#!/bin/sh
# Run by systemd after LocalHome stops (ExecStopPost, as root - see
# deploy/tune-board.sh step 8). When the stop was the watchdog's - LocalHome
# hung, no sign of life for WatchdogSec - it goes in the watchdog history
# the admin panel shows; LocalHome itself couldn't write it, being stuck.
if [ "$SERVICE_RESULT" = "watchdog" ]; then
    mkdir -p /var/lib/localhome
    echo "$(date '+%Y-%m-%d %H:%M:%S') killed" >> /var/lib/localhome/service-watchdog.log
    tail -n 200 /var/lib/localhome/service-watchdog.log > /var/lib/localhome/service-watchdog.log.tmp \
        && mv /var/lib/localhome/service-watchdog.log.tmp /var/lib/localhome/service-watchdog.log
    chmod 644 /var/lib/localhome/service-watchdog.log
fi
exit 0
