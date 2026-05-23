#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="/Users/mabdelouahab/mab_lab/mab_code/My_Daily_Slicer"
cd "$PROJECT_DIR"

if [[ -f .env ]]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

exec /usr/bin/env python3 trigger_topology.py trigger
