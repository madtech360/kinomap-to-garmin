#!/bin/sh
# MODE=trigger (default): wait for Home Assistant to call POST /run
# MODE=loop: run every INTERVAL seconds (default 15 minutes)
if [ "${MODE:-trigger}" = "loop" ]; then
    INTERVAL="${INTERVAL:-900}"
    while true; do
        python kinomap_to_garmin.py || echo "run failed with exit code $?"
        sleep "$INTERVAL"
    done
else
    exec python trigger_server.py
fi
