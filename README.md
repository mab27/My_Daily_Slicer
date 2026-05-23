# My Daily Slicer

A little helper that asks Jenkins for a fresh test topology every morning at
06:00 CET, so I always have one waiting when I sit down to work. Tracks the
sub-task **MAB-17**.

Each run kicks off `Give_me_a_topology` with `RUN_NAME=Dispo-<MMDD>` and waits
for Slicer to finish deploying it. That's the whole job.

## Getting started

```sh
pip install -r requirements.txt
```

Auth is optional. The Jenkins instance accepts anonymous triggers today, so the
script runs without credentials. If that changes, drop `JENKINS_USER` and
`JENKINS_TOKEN` (API token) into `.env`.

## Running it by hand

The script has four small steps. Run them one at a time, or chain them:

```sh
python3 trigger_topology.py trigger    # fire the build, save state/current.json
python3 trigger_topology.py wait       # wait for Jenkins to finish
python3 trigger_topology.py verify     # check Slicer has the topology
python3 trigger_topology.py monitor    # watch it deploy
```

End-to-end:

```sh
python3 trigger_topology.py trigger \
  && python3 trigger_topology.py wait \
  && python3 trigger_topology.py verify \
  && python3 trigger_topology.py monitor
```

`trigger` and `wait` are split on purpose: `trigger` is fast and writes the
build number to `state/current.json`, so a long `wait` can always pick up
where things left off.

### Picking an AOS version

By default the build runs from `AOS_latest_OB`. To pin a specific release:

```sh
python3 trigger_topology.py trigger --version AOS_6.1.0_OB
```

## What happens after the build

Slicer auto-deploys every topology Jenkins creates — there's no button to
press. The status walks itself through:

```
not_deployed → deploy_in_progress → deployed
```

- `verify` just checks the topology shows up in Slicer (any status counts).
- `monitor` follows it the rest of the way and gives up only if the status
  stops changing for 90 minutes.

## What the output looks like

A trimmed real run (build #72697, 2026-05-23):

```text
Triggered Dispo-0523, resolving build number...
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

After a successful run, `state/current.json` looks like this:

```json
{
  "name": "Dispo-0522",
  "build_number": 72659,
  "build_url": "https://jenkins.dc1.apstra.com/.../72659/",
  "triggered_at": "2026-05-22T17:15:40+02:00",
  "slicer_name": "zz-Dispo-0522-evpn_mlag.vex...",
  "build_finished_at": "2026-05-22T17:19:51+02:00",
  "verified_at": "2026-05-22T17:19:52+02:00",
  "deploy_started_at": "2026-05-22T18:04:32+02:00",
  "deployed_at": "2026-05-22T18:11:32+02:00"
}
```

## Running it on a schedule

### macOS (launchd)

```sh
cp scheduler/com.mab.daily-slicer.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.mab.daily-slicer.plist
```

This fires at 06:00 **local time**, so the machine needs to be on Europe/Paris.
Logs go to `scheduler/logs/`.

To stop it:

```sh
launchctl unload ~/Library/LaunchAgents/com.mab.daily-slicer.plist
```

### cron

```
0 6 * * * cd /Users/mabdelouahab/mab_lab/mab_code/My_Daily_Slicer && ./scheduler/run.sh
```
