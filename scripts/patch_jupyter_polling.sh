#!/bin/bash
set -euo pipefail

VENV="${1:-.venv}"
CHANNELS="$VENV/lib/python*/site-packages/jupyter_server/services/kernels/connection/channels.py"

for f in $CHANNELS; do
    if [ -f "$f" ]; then
        if grep -q 'call_later(0.5,' "$f"; then
            sed -i 's/call_later(0.5,/call_later(0.1,/g' "$f"
            echo "Patched nudge interval: 0.5s → 0.1s in $f"
        else
            echo "Already patched or different format in $f"
        fi
    fi
done
