#!/bin/sh
# One CSV line of the board's resources - run every 10 minutes by
# localhome-resource-log.timer, so a board left in the field for weeks
# comes back with the curve that says whether anything slowly grows:
# LocalHome's memory, threads and open files, its CPU time, the Linux-MCU
# bridge's and the broker's memory, the board's free memory, eMMC writes
# and the history database's size.
#
# ~17 KB a day; past a year of lines the oldest are dropped. Reading it:
#   column -t -s, ~/localhome-resource-log.csv | less
#
# Install (see docs/quick-installation-arduino-uno-q.md, "A resource log"):
#   sudo install -m 755 deploy/resource-log/localhome-resource-log.sh /usr/local/bin/
#   sudo install -m 644 deploy/resource-log/localhome-resource-log.service deploy/resource-log/localhome-resource-log.timer /etc/systemd/system/
#   sudo systemctl daemon-reload && sudo systemctl enable --now localhome-resource-log.timer
LOG=${LOCALHOME_RESOURCE_LOG:-$HOME/localhome-resource-log.csv}
DB=${LOCALHOME_HISTORY_DB:-$HOME/localhome/config/data/history.db}
MAX_LINES=52560   # a year at one line every 10 minutes

if [ ! -f "$LOG" ]; then
    echo "time,uptime_s,lh_rss_kb,lh_peak_kb,lh_threads,lh_fds,lh_cpu_s,router_rss_kb,mosquitto_rss_kb,mem_available_kb,emmc_sectors_written,history_db_bytes" > "$LOG"
fi

pid_of() { systemctl show -p MainPID --value "$1" 2>/dev/null; }
status_kb() { [ -n "$1" ] && [ "$1" != 0 ] && awk -v k="$2:" '$1==k {print $2}' "/proc/$1/status" 2>/dev/null; }

lh=$(pid_of localhome)
cpu=""
fds=""
if [ -n "$lh" ] && [ "$lh" != 0 ]; then
    cpu=$(awk -v t="$(getconf CLK_TCK)" '{printf "%.1f", ($14+$15)/t}' "/proc/$lh/stat" 2>/dev/null)
    fds=$(ls "/proc/$lh/fd" 2>/dev/null | wc -l)
fi
db=$(( $(stat -c %s "$DB" 2>/dev/null || echo 0) + $(stat -c %s "$DB-wal" 2>/dev/null || echo 0) ))

echo "$(date +%Y-%m-%dT%H:%M:%S),$(cut -d. -f1 /proc/uptime | cut -d' ' -f1),$(status_kb "$lh" VmRSS),$(status_kb "$lh" VmHWM),$(status_kb "$lh" Threads),$fds,$cpu,$(status_kb "$(pid_of arduino-router)" VmRSS),$(status_kb "$(pid_of mosquitto)" VmRSS),$(awk '$1=="MemAvailable:" {print $2}' /proc/meminfo),$(awk '$3=="mmcblk0" {print $10}' /proc/diskstats),$db" >> "$LOG"

if [ "$(wc -l < "$LOG")" -gt "$MAX_LINES" ]; then
    { head -1 "$LOG"; tail -n "$MAX_LINES" "$LOG" | tail -n +2; } > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
