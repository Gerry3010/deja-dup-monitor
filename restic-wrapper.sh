#!/usr/bin/env bash
# restic wrapper for deja-dup-monitor
# Tees restic's JSON output to the log file so the monitor can read it,
# while keeping stdout/stderr intact for deja-dup.

REAL_RESTIC="/usr/bin/restic"
LOG_FILE="$HOME/.cache/deja-dup/restic.log"

# Tee stdout to log (JSON progress lines go here)
# Tee stderr to log as well (error lines), while keeping it on stderr
"$REAL_RESTIC" "$@" \
    2> >(tee -a "$LOG_FILE" >&2) \
    | tee -a "$LOG_FILE"

exit "${PIPESTATUS[0]}"
