# My Daily Slicer

Daily disposable Jenkins topology provisioner. Sub-task **MAB-17**: trigger
`Give_me_a_topology` at 06:00 CET with `RUN_NAME=Dispo-<MMDD>`.

## Setup

```sh
pip install -r requirements.txt
```

The Jenkins instance appears to accept anonymous job triggers (the captured
curl in MAB-17 had no `Authorization` header and still got a 303). The script
defaults to anonymous; if Jenkins ever starts requiring auth, set
`JENKINS_USER` and `JENKINS_TOKEN` (API token, not password) in `.env`.

## Manual run

```sh
python3 trigger_topology.py trigger    # fires the build, records state/current.json, exits
python3 trigger_topology.py wait       # polls until the recorded build finishes
python3 trigger_topology.py verify     # confirms topology is registered with Slicer
python3 trigger_topology.py monitor    # polls deploy_status until it reaches 'deployed'
# end-to-end:
python3 trigger_topology.py trigger \
  && python3 trigger_topology.py wait \
  && python3 trigger_topology.py verify \
  && python3 trigger_topology.py monitor
```

`trigger` is fast — it submits the build and resolves the queue item to a real
build number, then writes `state/current.json`. `wait` reads that file and
polls the build's `/api/json` until `building == false`. The two are
deliberately separate so a long wait can't lose track of what was triggered.

Slicer auto-deploys every topology that Jenkins creates — there is no manual
"Deploy" step. The status progresses on its own through
`not_deployed → deploy_in_progress → deployed`. `verify` only confirms Slicer
has registered the topology (it accepts any of those states), and `monitor`
just watches the progression. `monitor` uses an idle timeout
(`MONITOR_POLL_TIMEOUT_S`, default 45 min) — it errors out only if the status
stops changing.

## Sample output

Real end-to-end run on 2026-05-22 (build #72659):

```text
$ python3 trigger_topology.py trigger
Triggered Dispo-0522, resolving build number...
Build #72659 started: https://jenkins.dc1.apstra.com/job/ptest/job/Give_me_a_topology/72659/

$ python3 trigger_topology.py wait
Waiting on Dispo-0522 (build #72659): https://jenkins.dc1.apstra.com/job/ptest/job/Give_me_a_topology/72659/
  building... (0m 1s elapsed)
  building... (0m 31s elapsed)
  ...
  building... (3m 34s elapsed)
Finished: SUCCESS (took 3m 44s)
Topology: zz-Dispo-0522-evpn_mlag.vex.2485377892354-2603053242

$ python3 trigger_topology.py verify
Verifying topology zz-Dispo-0522-evpn_mlag.vex.2485377892354-2603053242
Verified: zz-Dispo-0522-evpn_mlag.vex.2485377892354-2603053242 present in Slicer (deploy_status=not_deployed)

# `verify -v` (or `--verbose`) additionally prints the full Slicer payload
# (~130 KB JSON of deploy_model.devices + reservation fields).

$ python3 trigger_topology.py monitor
Monitoring deploy_status of zz-Dispo-0522-evpn_mlag.vex.2485377892354-2603053242
  [0m 0s] deploy_status -> 'not_deployed'
  not_deployed... (2m 0s elapsed)
  [4m 0s] deploy_status -> 'deploy_in_progress'
  deploy_in_progress... (6m 0s elapsed)
  deploy_in_progress... (8m 0s elapsed)
  [10m 0s] deploy_status -> 'deployed'
Deployed in 10m 0s
```

How to read it:

- `wait` prints a `building...` heartbeat every 30s while Jenkins runs, then
  one `Finished: <result>` line and the topology name parsed from the console
  log (`Creating systest: …`).
- `verify` succeeds as soon as Slicer returns the topology — `deploy_status`
  may already be `deploy_in_progress` or even `deployed` since Slicer
  auto-progresses without external trigger. Missing topologies (404) are
  retried until `SLICER_POLL_TIMEOUT_S`.
- `monitor` polls every 2 min (`MONITOR_POLL_INTERVAL_S = 120`) — the deploy
  phase takes minutes, so frequent polling adds noise without value. Lines
  come in two flavours: a **transition** line `  [Xm Ys] deploy_status -> 'new'`
  whenever the status changes, and a **heartbeat** line
  `  <status>... (Xm Ys elapsed)` on every poll that didn't see a change. The
  final `Deployed in Xm Ys` reports total elapsed monitor time.

State after the run (`state/current.json`):

```json
{
  "name": "Dispo-0522",
  "build_number": 72659,
  "build_url": "https://jenkins.dc1.apstra.com/job/ptest/job/Give_me_a_topology/72659/",
  "triggered_at": "2026-05-22T17:15:40+02:00",
  "slicer_name": "zz-Dispo-0522-evpn_mlag.vex.2485377892354-2603053242",
  "build_finished_at": "2026-05-22T17:19:51+02:00",
  "verified_at": "2026-05-22T17:19:52+02:00",
  "deploy_started_at": "2026-05-22T18:04:32+02:00",
  "deployed_at": "2026-05-22T18:11:32+02:00"
}
```

## Daily at 06:00 (macOS launchd)

```sh
cp scheduler/com.mab.daily-slicer.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.mab.daily-slicer.plist
```

`StartCalendarInterval` fires at 06:00 **local time** — make sure the machine's
timezone is Europe/Paris (CET/CEST). Logs land in `scheduler/logs/`.

To unload: `launchctl unload ~/Library/LaunchAgents/com.mab.daily-slicer.plist`.

## cron equivalent

```
0 6 * * * cd /Users/mabdelouahab/mab_lab/mab_code/My_Daily_Slicer && ./scheduler/run.sh
```
