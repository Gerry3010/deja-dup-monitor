#!/usr/bin/env bash
# restic wrapper for deja-dup-monitor
#
# Tees restic's stdout (JSON progress) to ~/.cache/deja-dup/restic.log.
# stderr flows unchanged to deja-dup.
#
# Why only stdout? Bash process substitutions (2> >(tee ...)) keep the
# write-end of the pipe open after restic exits, causing the wrapper to hang
# and deja-dup to never detect backup completion.
#
# Installation:
#   cp restic-wrapper.sh ~/.local/bin/restic
#   chmod +x ~/.local/bin/restic

REAL_RESTIC="/usr/bin/restic"
LOG_FILE="$HOME/.cache/deja-dup/restic.log"

"$REAL_RESTIC" "$@" | tee -a "$LOG_FILE"
exit "${PIPESTATUS[0]}"
