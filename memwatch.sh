#!/usr/bin/env bash
# memwatch.sh TIME_PID: pause the run (exact PIDs: the time wrapper's process tree) when MemAvailable < 4 GB, resume > 6 GB.
ROOT=$1; paused=0
tree() { local p=$1; echo $p; for c in $(cat /proc/$p/task/*/children 2>/dev/null); do tree $c; done; }
while kill -0 $ROOT 2>/dev/null; do
  av=$(awk '/MemAvailable/ {print int($2/1048576)}' /proc/meminfo)
  if [ $paused = 0 ] && [ $av -lt 4 ]; then for p in $(tree $ROOT); do kill -STOP $p; done; paused=1; echo "$(date +%T) paused at ${av} GB"; fi
  if [ $paused = 1 ] && [ $av -ge 6 ]; then for p in $(tree $ROOT); do kill -CONT $p; done; paused=0; echo "$(date +%T) resumed at ${av} GB"; fi
  sleep 10
done
