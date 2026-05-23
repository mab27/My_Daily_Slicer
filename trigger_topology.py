#!/usr/bin/env python3
"""Provision and track a daily disposable Jenkins topology.

Subcommands:
  trigger  Fire the Jenkins build, resolve the queue item to a build number,
           save state to state/current.json. Fast.
  wait     Read state, poll until the build finishes. Long-running (~20 min).
  verify   Confirm the topology is registered with Slicer (any deploy_status).
  monitor  Poll Slicer deploy_status until it reaches 'deployed'. Slicer
           auto-progresses the topology through not_deployed ->
           deploy_in_progress -> deployed without any external trigger.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from urllib3.exceptions import InsecureRequestWarning

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

JENKINS_BASE = "https://jenkins.dc1.apstra.com"
JOB_PATH = "/view/ptest/job/ptest/job/Give_me_a_topology"
SLICER_BASE = "http://slicer-topology-management-ui.k8s-autobuild.dc1.apstra.com"
SLICER_OWNER = "mabdelouahab@juniper.net"
CET = ZoneInfo("Europe/Paris")

STATE_DIR = Path(__file__).resolve().parent / "state"
STATE_FILE = STATE_DIR / "current.json"

QUEUE_POLL_INTERVAL_S = 2
QUEUE_POLL_TIMEOUT_S = 120
BUILD_POLL_INTERVAL_S = 30
BUILD_POLL_TIMEOUT_S = 30 * 60
SLICER_POLL_INTERVAL_S = 20
SLICER_POLL_TIMEOUT_S = 10 * 60
MONITOR_POLL_INTERVAL_S = 120
MONITOR_POLL_TIMEOUT_S = 45 * 60

SLICER_NAME_RE = re.compile(r"Creating systest:\s+(zz-Dispo-\S+)")

SLICER_FIELDS = [
    "name", "owner",
    "deploy_model.dutmgmt_connectivity",
    "deploy_model.dutmgmt_connectivity_v6",
    "deploy_model.mgmt_subnet",
    "deploy_model.mgmt_subnet_v6",
    "deploy_status",
    "reservation.created_at",
    "reservation.expires_at",
    "description",
    "reservation_duration",
    "reservation.uid",
    "reservation.owner",
    "reservation.status",
    "reservation.updated_at",
    "deploy_model.devices",
]

DEFAULT_PARAMS = {
    "SYSTEST_BRANCH": "master",
    "RUN_FROM_BRANCH": "AOS_latest_OB",
    "RUN_FROM_BUILD": "lastSuccessfulBuild",
    "TARGET_BUILD": "AOS_latest_OB",
    "TESTPLAN_FILE": "aptest/plans/all_ptests.plan",
    "EMAIL": "mehdi@apstra.com",
    "PTEST_NAME": "evpn_mlag.vex",
    "DO_NOT_DELETE": "true",
    "DEFAULT_VERSIONS": "",
    "PTEST_PRIORITY": "100",
    "EXISTING_TOPOLOGY": "",
    "SUITE_ARGS": "",
    "KEEP_ON_FAILURE": "false",
    "EXIT_ON_FAILURE": "false",
    "ENABLE_SENTRY": "false",
    "ENABLE_LOGSTASH": "false",
    "HOTPATCHES": "",
    "DRY_RUN": "false",
    "PYTHON3": "1",
    "ENABLE_ERROR_ACTIVITY_HISTOGRAM": "false",
}


def run_name(now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now(CET)
    return f"Dispo-{now.strftime('%m%d')}"


def make_session(user: str | None, token: str | None) -> requests.Session:
    s = requests.Session()
    if user and token:
        s.auth = (user, token)
    return s


def crumb_headers(session: requests.Session) -> dict[str, str]:
    resp = session.get(f"{JENKINS_BASE}/crumbIssuer/api/json", timeout=30)
    if resp.ok:
        crumb = resp.json()
        return {crumb["crumbRequestField"]: crumb["crumb"]}
    if resp.status_code == 404:
        return {}
    resp.raise_for_status()
    return {}


def resolve_queue_item(session: requests.Session, queue_url: str) -> tuple[int, str]:
    deadline = time.monotonic() + QUEUE_POLL_TIMEOUT_S
    while time.monotonic() < deadline:
        resp = session.get(f"{queue_url}api/json", timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("cancelled"):
            raise RuntimeError(f"Queue item {queue_url} was cancelled before starting")
        executable = data.get("executable")
        if executable:
            return executable["number"], executable["url"]
        time.sleep(QUEUE_POLL_INTERVAL_S)
    raise TimeoutError(f"Queue item {queue_url} did not start within {QUEUE_POLL_TIMEOUT_S}s")


def cmd_trigger(args: argparse.Namespace) -> int:
    session = make_session(args.user, args.token)
    name = args.name or run_name()
    params = {**DEFAULT_PARAMS, "RUN_NAME": name}

    resp = session.post(
        f"{JENKINS_BASE}{JOB_PATH}/buildWithParameters",
        params=params,
        headers=crumb_headers(session),
        timeout=30,
        allow_redirects=False,
    )
    resp.raise_for_status()
    queue_url = resp.headers["Location"]

    print(f"Triggered {name}, resolving build number...")
    build_number, build_url = resolve_queue_item(session, queue_url)

    STATE_DIR.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps({
        "name": name,
        "build_number": build_number,
        "build_url": build_url,
        "triggered_at": dt.datetime.now(CET).isoformat(timespec="seconds"),
    }, indent=2) + "\n")

    print(f"Build #{build_number} started: {build_url}")
    return 0


def cmd_wait(args: argparse.Namespace) -> int:
    if not STATE_FILE.exists():
        print(f"ERROR: no state file at {STATE_FILE}. Run `trigger` first.", file=sys.stderr)
        return 2
    state = json.loads(STATE_FILE.read_text())
    name = state["name"]
    build_url = state["build_url"]
    build_number = state["build_number"]

    session = make_session(args.user, args.token)
    print(f"Waiting on {name} (build #{build_number}): {build_url}", flush=True)
    deadline = time.monotonic() + BUILD_POLL_TIMEOUT_S
    started = time.monotonic()
    while time.monotonic() < deadline:
        resp = session.get(f"{build_url}api/json", timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("building"):
            result = data.get("result", "UNKNOWN")
            duration_s = data.get("duration", 0) // 1000
            print(f"Finished: {result} (took {duration_s // 60}m {duration_s % 60}s)", flush=True)
            if result == "SUCCESS":
                slicer_name = extract_slicer_name(session, build_url)
                if slicer_name:
                    print(f"Topology: {slicer_name}", flush=True)
                    state["slicer_name"] = slicer_name
                    state["build_finished_at"] = dt.datetime.now(CET).isoformat(timespec="seconds")
                    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")
                else:
                    print("WARN: could not find topology name in console log", file=sys.stderr, flush=True)
            return 0 if result == "SUCCESS" else 1
        elapsed = int(time.monotonic() - started)
        print(f"  building... ({elapsed // 60}m {elapsed % 60}s elapsed)", flush=True)
        time.sleep(BUILD_POLL_INTERVAL_S)
    print(f"ERROR: build did not finish within {BUILD_POLL_TIMEOUT_S // 60} minutes", file=sys.stderr, flush=True)
    return 2


def extract_slicer_name(session: requests.Session, build_url: str) -> str | None:
    resp = session.get(f"{build_url}consoleText", timeout=60)
    resp.raise_for_status()
    m = SLICER_NAME_RE.search(resp.text)
    return m.group(1) if m else None


def cmd_verify(args: argparse.Namespace) -> int:
    name = args.topology
    if not name and STATE_FILE.exists():
        name = json.loads(STATE_FILE.read_text()).get("slicer_name")
    if not name:
        print("ERROR: topology name not provided and not found in state (run `wait` first)", file=sys.stderr)
        return 2
    url = f"{SLICER_BASE}/v1_1/systest/{name}"
    headers = {"owner": SLICER_OWNER, "Content-Type": "application/json"}
    params = [("field", f) for f in SLICER_FIELDS]

    print(f"Verifying topology {name}", flush=True)
    deadline = time.monotonic() + SLICER_POLL_TIMEOUT_S
    started = time.monotonic()
    while time.monotonic() < deadline:
        resp = requests.get(url, headers=headers, params=params, timeout=30, verify=False)
        if resp.status_code == 404:
            elapsed = int(time.monotonic() - started)
            print(f"  not in Slicer yet... ({elapsed // 60}m {elapsed % 60}s elapsed)", flush=True)
            time.sleep(SLICER_POLL_INTERVAL_S)
            continue
        resp.raise_for_status()
        data = resp.json()
        status = (data.get("deploy_status") or "").lower()
        if status:
            print(f"Verified: {data['name']} present in Slicer (deploy_status={status})", flush=True)
            if args.verbose:
                print(json.dumps(data, indent=2), flush=True)
            if STATE_FILE.exists():
                state = json.loads(STATE_FILE.read_text())
                state["slicer_name"] = data["name"]
                state["verified_at"] = dt.datetime.now(CET).isoformat(timespec="seconds")
                STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")
            return 0
        print(f"  empty deploy_status, retrying...", flush=True)
        time.sleep(SLICER_POLL_INTERVAL_S)
    print(f"ERROR: topology not verified within {SLICER_POLL_TIMEOUT_S // 60} minutes", file=sys.stderr, flush=True)
    return 2


def cmd_monitor(args: argparse.Namespace) -> int:
    name = args.topology
    if not name and STATE_FILE.exists():
        name = json.loads(STATE_FILE.read_text()).get("slicer_name")
    if not name:
        print("ERROR: topology name not provided and not found in state (run `wait` first)", file=sys.stderr)
        return 2
    url = f"{SLICER_BASE}/v1_1/systest/{name}"
    headers = {"owner": SLICER_OWNER, "Content-Type": "application/json"}
    params = [("field", f) for f in SLICER_FIELDS]

    print(f"Monitoring deploy_status of {name}", flush=True)
    started = time.monotonic()
    last_change = started
    last_status: str | None = None
    while time.monotonic() - last_change < MONITOR_POLL_TIMEOUT_S:
        resp = requests.get(url, headers=headers, params=params, timeout=30, verify=False)
        if resp.status_code == 404:
            print(f"ERROR: topology {name} no longer exists in Slicer (404) — was it deleted?", file=sys.stderr, flush=True)
            return 2
        resp.raise_for_status()
        data = resp.json()
        status = (data.get("deploy_status") or "").lower()
        elapsed = int(time.monotonic() - started)
        if status != last_status:
            print(f"  [{elapsed // 60}m {elapsed % 60}s] deploy_status -> {status!r}", flush=True)
            record_status_transition(status)
            last_status = status
            last_change = time.monotonic()
        else:
            print(f"  {status}... ({elapsed // 60}m {elapsed % 60}s elapsed)", flush=True)
        if status == "deployed":
            print(f"Deployed in {elapsed // 60}m {elapsed % 60}s", flush=True)
            return 0
        time.sleep(MONITOR_POLL_INTERVAL_S)
    print(f"ERROR: deploy_status stuck at {last_status!r} for {MONITOR_POLL_TIMEOUT_S // 60} minutes", file=sys.stderr, flush=True)
    return 2


def record_status_transition(status: str) -> None:
    if not STATE_FILE.exists():
        return
    state = json.loads(STATE_FILE.read_text())
    now = dt.datetime.now(CET).isoformat(timespec="seconds")
    key = {
        "deploy_in_progress": "deploy_started_at",
        "deployed": "deployed_at",
    }.get(status)
    if key and key not in state:
        state[key] = now
        STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--user", default=os.environ.get("JENKINS_USER") or None)
    p.add_argument("--token", default=os.environ.get("JENKINS_TOKEN") or None)

    sub = p.add_subparsers(dest="cmd", required=True)

    trig = sub.add_parser("trigger", help="Fire the build and record state")
    trig.add_argument("--name", help="Override RUN_NAME (default: Dispo-<MMDD> in CET)")
    trig.set_defaults(func=cmd_trigger)

    wait = sub.add_parser("wait", help="Poll until the recorded build finishes")
    wait.set_defaults(func=cmd_wait)

    verify = sub.add_parser("verify", help="Confirm topology is registered with Slicer (any deploy_status)")
    verify.add_argument("topology", nargs="?", help="Full Slicer topology name; defaults to state['slicer_name']")
    verify.add_argument("--verbose", "-v", action="store_true", help="Also print the full Slicer payload (large)")
    verify.set_defaults(func=cmd_verify)

    monitor = sub.add_parser("monitor", help="Poll topology deploy_status until it reaches 'deployed'")
    monitor.add_argument("topology", nargs="?", help="Full Slicer topology name; defaults to state['slicer_name']")
    monitor.set_defaults(func=cmd_monitor)

    return p


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.func(args)
    except requests.HTTPError as e:
        url = e.response.url if e.response is not None else "?"
        code = e.response.status_code if e.response is not None else "?"
        body = e.response.text[:500] if e.response is not None else ""
        print(f"ERROR: HTTP {code} from {url}: {body}", file=sys.stderr)
        return 1
    except (RuntimeError, TimeoutError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
