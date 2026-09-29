#!/bin/sh
# Run by systemd after LocalHome stops (ExecStopPost, as root - see
# deploy/tune-board.sh step 8). When the stop was the watchdog's - LocalHome
# hung, no sign of life for WatchdogSec - or it outgrew its MemoryMax, it
# goes in the watchdog history the admin panel shows; LocalHome itself
# couldn't write it, being stuck or already gone.
case "$SERVICE_RESULT" in
    watchdog) code=killed ;;
    oom-kill) code=memory ;;
    *) exit 0 ;;
esac
if [ -n "$code" ]; then
    mkdir -p /var/lib/localhome
    echo "$(date '+%Y-%m-%d %H:%M:%S') $code" >> /var/lib/localhome/service-watchdog.log
    tail -n 200 /var/lib/localhome/service-watchdog.log > /var/lib/localhome/service-watchdog.log.tmp \
        && mv /var/lib/localhome/service-watchdog.log.tmp /var/lib/localhome/service-watchdog.log
    chmod 644 /var/lib/localhome/service-watchdog.log
fi
exit 0
