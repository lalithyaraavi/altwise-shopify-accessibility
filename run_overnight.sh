#!/bin/bash
# Runs every collection step in order, keeps the Mac awake until done, and logs everything.
# Start it with:   bash run_overnight.sh
# Each step only does what's left, so it's safe to run again if it stops.
cd "$(dirname "$0")"
LOG="logs/overnight_$(date +%Y-%m-%d_%H%M).log"
echo "Logging to $LOG"
caffeinate -i bash -c '
  echo "=== 1/4 scrape new stores    $(date) ===";  python3 -u scrape_products.py
  echo "=== 2/4 fetch alt text       $(date) ===";  python3 -u fetch_alt.py
  echo "=== 3/4 page checks (2/store) $(date) ===";  python3 -u page_check.py
  echo "=== 4/4 rebuild tables       $(date) ===";  python3 -u build_tables.py
  echo "=== ALL DONE                 $(date) ==="
' 2>&1 | tee "$LOG"
