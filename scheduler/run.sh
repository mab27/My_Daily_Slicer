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

# Topology 1: evpn_mlag.vex (auto-name)
python3 trigger_topology.py trigger

# Topology 2: ai_scale.extra_small_evpn
python3 trigger_topology.py trigger --name "Dispo-$(date +%m%d)-ai_scale" --ptest ai_scale.extra_small_evpn
