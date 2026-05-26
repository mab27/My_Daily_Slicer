# My Daily Slicer

A little helper that asks Jenkins for fresh test topologies every morning and
tears them all down again at night, so I always have one (or several) waiting
when I sit down to work and nothing lingering when I don't. Tracks parent tasks
**MAB-15** (provision) and **MAB-16** (deprovision).

Each provision run kicks off `Give_me_a_topology` with a custom `RUN_NAME` and
waits for Slicer to finish deploying it. Each teardown run posts Slicer's
`undeploy` endpoint for every still-live topology and watches each one walk
back to `not_deployed`.

## State model

State is per-topology, one JSON file per `run_name` under `state/`:

```
state/Dispo-0524.json
state/Dispo-0524-b.json
state/Dispo-0524-evpn.json
```

Every subcommand that acts on an *existing* topology (`wait`, `verify`,
`monitor`, `undeploy`, `monitor-undeploy`) requires `--name <run_name>` — no
implicit selection. `trigger` is the one exception: `--name` is optional and
defaults to `Dispo-<MMDD>` in CET. If that file already exists, `trigger`
refuses to overwrite it and asks you to pass an explicit `--name` (for the
second-or-later topology of the day). `undeploy` and `monitor-undeploy` also
accept `--all` for the daily teardown sweep.

## Getting started

```sh
pip install -r requirements.txt
```

Auth is optional. The Jenkins instance accepts anonymous triggers today, so the
script runs without credentials. If that changes, drop `JENKINS_USER` and
`JENKINS_TOKEN` (API token) into `.env`.

### Important: Slicer topology TTL

Topologies have a limited lifetime in Slicer and auto-delete after a retention
period (typically hours). **Run the full provision chain (`trigger` → `wait` →
`verify` → `monitor`) without long delays** to avoid the topology expiring
mid-workflow. The script detects TTL expiration and gives a clear error
message, but the safest approach is to chain the commands immediately.

## Daily workflow

**Morning (once per day):**
```sh
# Create a fresh topology (auto-named Dispo-<today's date>)
python3 trigger_topology.py trigger

# Then run the provision chain
NAME=$(python3 trigger_topology.py latest)
python3 trigger_topology.py wait    --name "$NAME" \
  && python3 trigger_topology.py verify  --name "$NAME" \
  && python3 trigger_topology.py monitor --name "$NAME"
```

**Evening (once per day):**
```sh
# Undeploy all topologies
python3 trigger_topology.py undeploy --all \
  && python3 trigger_topology.py monitor-undeploy --all \
  && python3 trigger_topology.py delete --all
```

**Automated:** Both run automatically at 06:00 CET (provision) and 23:30 CET
(teardown) if you load the launchd plists. See [Running it on a
schedule](#running-it-on-a-schedule).

## Running it by hand

A provision run is four small steps. The default name is `Dispo-<MMDD>`. Run
`trigger`, then copy-paste the `Next:` block that appears **immediately**
(without long delays):

```sh
python3 trigger_topology.py trigger

# Output shows:
#   Next:
#     python3 trigger_topology.py wait    --name Dispo-0524
#     python3 trigger_topology.py verify  --name Dispo-0524
#     python3 trigger_topology.py monitor --name Dispo-0524
```

Or chain them programmatically with `latest` (recommended):

```sh
NAME=$(python3 trigger_topology.py latest)
python3 trigger_topology.py wait    --name "$NAME" \
  && python3 trigger_topology.py verify  --name "$NAME" \
  && python3 trigger_topology.py monitor --name "$NAME"
```

End-to-end (explicit name):

```sh
NAME=Dispo-0524
python3 trigger_topology.py trigger --name "$NAME" \
  && python3 trigger_topology.py wait    --name "$NAME" \
  && python3 trigger_topology.py verify  --name "$NAME" \
  && python3 trigger_topology.py monitor --name "$NAME"
```

**Why no delays?** Slicer topologies have a limited TTL and auto-delete after a
retention period. Running the chain immediately ensures the topology lives long
enough to complete deployment. If delays are unavoidable and the topology
expires, you'll get a clear error message; just trigger a fresh one.

For a second topology the same day, pass an explicit `--name` on `trigger`
(otherwise it refuses to overwrite today's state file):

```sh
python3 trigger_topology.py trigger --name Dispo-0524-evpn611 --version AOS_6.1.0_OB
```

`trigger` and `wait` are split on purpose: `trigger` is fast and writes the
build number to `state/<name>.json`, so if you interrupt and come back hours
later, `wait` can pick up where things left off (but be aware the topology may
have expired from Slicer by then).

### Picking an AOS version or PTEST

By default the build runs from `AOS_latest_OB` with `PTEST_NAME=evpn_mlag.vex`.
Override either or both:

```sh
python3 trigger_topology.py trigger \
  --name Dispo-0524-evpn611 \
  --version AOS_6.1.0_OB \
  --ptest evpn_mlag.vex
```

### Tearing things down

Three-step teardown: undeploy → monitor → delete.

Single topology:
```sh
python3 trigger_topology.py undeploy         --name Dispo-0524     # POST undeploy request
python3 trigger_topology.py monitor-undeploy --name Dispo-0524     # poll until not_deployed
python3 trigger_topology.py delete           --name Dispo-0524     # DELETE record from Slicer
```

Sweep every live topology in one go (idempotent):
```sh
python3 trigger_topology.py undeploy --all \
  && python3 trigger_topology.py monitor-undeploy --all \
  && python3 trigger_topology.py delete --all
```

Each command skips topologies already in that stage, so re-running is safe:
- `undeploy --all` skips if `undeploy_requested_at` exists
- `monitor-undeploy --all` skips if `undeployed_at` exists  
- `delete --all` skips if `deleted_at` exists

**Note on delete timing:** Slicer may return 412 (resources still releasing) immediately after undeploy. If this happens, retry `delete` after a brief wait (resources usually release within seconds).

### See what's tracked

```sh
python3 trigger_topology.py list           # show all topologies and their stage
python3 trigger_topology.py latest         # print most-recently triggered name (for scripting)
```

`latest` output (run_name for use with `--name`):
```
Dispo-0526
```

`list` output (full table with slicer topology names):
```
NAME                   VERSION            PTEST                  SLICER_NAME                                                  STAGE                  AT
Dispo-0523             AOS_6.1.0_OB       evpn_mlag.vex          zz-Dispo-0523-evpn_mlag.vex.2485377892354-961841954         undeployed             2026-05-23T23:42:11+02:00
Dispo-0524             AOS_latest_OB      evpn_mlag.vex          zz-Dispo-0524-evpn_mlag.vex.2485377892354-2885040704        deployed               2026-05-24T06:51:14+02:00
Dispo-0524-evpn611     AOS_6.1.0_OB       evpn_mlag.vex          zz-Dispo-0524-evpn611-evpn_mlag.vex.2485377892354-1234567   deploy_started         2026-05-24T07:02:33+02:00
```

The `SLICER_NAME` column shows the full Slicer topology identifier, useful for API calls or manual UI lookups. It appears as `-` until the topology is created (`wait` completes).

## What happens after the build

Slicer auto-deploys every topology Jenkins creates — there's no button to
press. The status walks itself through:

```
not_deployed → deploy_in_progress → deployed → undeploy_in_progress → not_deployed
```

- `verify` just checks the topology shows up in Slicer (any status counts).
- `monitor` follows it the rest of the way to `deployed` and gives up only if
  the status stops changing for 90 minutes.
- `monitor-undeploy` watches the back half of the cycle and exits once the
  topology returns to `not_deployed` (the record stays in Slicer; it does not
  404).

## What the output looks like

A trimmed real provision run (build #72697, 2026-05-23):

```text
Triggered Dispo-0523 (RUN_FROM_BRANCH=AOS_latest_OB, PTEST_NAME=evpn_mlag.vex), resolving build number...
Build #72697 started: https://jenkins.dc1.apstra.com/.../72697/
Waiting on Dispo-0523 (build #72697)
  building... (0m 32s elapsed)
  building... (2m 3s elapsed)
  building... (5m 6s elapsed)
Finished: SUCCESS (took 5m 56s)
Topology: zz-Dispo-0523-evpn_mlag.vex.2485377892354-961841954

Verifying topology zz-Dispo-0523-evpn_mlag.vex...
Verified: present in Slicer (deploy_status=not_deployed)

Monitoring deploy_status of zz-Dispo-0523-...
  [0m 0s] deploy_status -> 'not_deployed'
  not_deployed... (10m 5s elapsed)
  [44m 22s] deploy_status -> 'deploy_in_progress'
  [51m 14s] deploy_status -> 'deployed'
Deployed in 51m 14s
```

A couple of things to know when reading it:

- `wait` heartbeats every 30s while Jenkins runs, then prints the topology
  name it scraped from the console log.
- `monitor` polls every 2 minutes. You'll see a **transition** line whenever
  the status changes, and a quieter **heartbeat** line otherwise.
- `monitor-undeploy --all` polls each pending topology every 2 minutes too,
  printing one line per topology per transition.

After a successful provision + teardown, `state/Dispo-0524.json` looks like:

```json
{
  "name": "Dispo-0524",
  "version": "AOS_latest_OB",
  "ptest": "evpn_mlag.vex",
  "build_number": 72702,
  "build_url": "https://jenkins.dc1.apstra.com/.../72702/",
  "triggered_at": "2026-05-24T06:00:01+02:00",
  "slicer_name": "zz-Dispo-0524-evpn_mlag.vex...",
  "build_finished_at": "2026-05-24T06:05:51+02:00",
  "verified_at": "2026-05-24T06:05:52+02:00",
  "deploy_started_at": "2026-05-24T06:48:32+02:00",
  "deployed_at": "2026-05-24T06:55:11+02:00",
  "undeploy_requested_at": "2026-05-24T23:30:02+02:00",
  "undeploy_started_at": "2026-05-24T23:30:42+02:00",
  "undeployed_at": "2026-05-24T23:34:18+02:00",
  "deleted_at": "2026-05-24T23:35:42+02:00"
}
```

## Understanding the state file

Each topology gets a JSON file under `state/<run_name>.json`. Fields record the lifecycle:

| Field | Set by | Meaning |
|-------|--------|---------|
| `name`, `version`, `ptest` | `trigger` | Provision parameters |
| `build_number`, `build_url` | `trigger` | Jenkins build link |
| `slicer_name` | `wait` | Full topology name in Slicer (auto-generated by Jenkins) |
| `triggered_at` | `trigger` | When the build was fired |
| `build_finished_at`, `verified_at`, `deploy_started_at`, `deployed_at` | provision chain | Timestamps marking each provision milestone |
| `undeploy_requested_at`, `undeploy_started_at`, `undeployed_at` | deprovision chain | Timestamps marking teardown: requested, started, completed |
| `deleted_at` | `delete` | When the topology record was deleted from Slicer |

Missing fields are normal — they get filled in as the topology moves through its lifecycle. You can inspect any state file to understand where it is in the workflow (e.g., a file with only `triggered_at` and `build_finished_at` is still waiting for `verify`).

## Running it on a schedule

Two launchd jobs ship with this repo: one fires the daily provision at 06:00,
the other tears everything down at 23:30. Both fire in **local time**, so the
machine needs to be on Europe/Paris. Logs go to `scheduler/logs/`.

### macOS (launchd)

```sh
cp scheduler/com.mab.daily-slicer.plist           ~/Library/LaunchAgents/
cp scheduler/com.mab.daily-slicer-undeploy.plist  ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.mab.daily-slicer.plist
launchctl load ~/Library/LaunchAgents/com.mab.daily-slicer-undeploy.plist
```

The provision job runs `scheduler/run.sh`, which fires `trigger` calls. By
default it provisions one topology with the auto-name `Dispo-<MMDD>`. To
provision several topologies per day, edit `run.sh` to add more `trigger`
calls with distinct `--name` and `--ptest` as needed:

```bash
#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="/Users/mabdelouahab/mab_lab/mab_code/My_Daily_Slicer"
cd "$PROJECT_DIR"

if [[ -f .env ]]; then
    set -a
    source .env
    set +a
fi

# Topology 1: evpn_mlag.vex (auto-name)
python3 trigger_topology.py trigger

# Topology 2: ai_scale.extra_small_evpn
python3 trigger_topology.py trigger --name "Dispo-$(date +%m%d)-ai_scale" --ptest ai_scale.extra_small_evpn
```

Both fire at 06:00 CET, provisioning `Dispo-0524.json` and
`Dispo-0524-ai_scale.json` in parallel (Jenkins builds are independent).

The teardown job runs `scheduler/run-undeploy.sh`, which sweeps every state
file with `undeploy --all && monitor-undeploy --all`. It is name-agnostic by
design — anything still live at 23:30 gets torn down.

To stop either job:

```sh
launchctl unload ~/Library/LaunchAgents/com.mab.daily-slicer.plist
launchctl unload ~/Library/LaunchAgents/com.mab.daily-slicer-undeploy.plist
```

### cron

```
0  6 * * * cd /Users/mabdelouahab/mab_lab/mab_code/My_Daily_Slicer && ./scheduler/run.sh
30 23 * * * cd /Users/mabdelouahab/mab_lab/mab_code/My_Daily_Slicer && ./scheduler/run-undeploy.sh
```
