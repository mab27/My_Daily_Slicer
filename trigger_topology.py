#!/usr/bin/env python3
"""Provision, track, and tear down disposable Jenkins/Slicer topologies.

State lives one-file-per-topology under `state/<run_name>.json`; every
subcommand requires `--name` (no implicit selection). `--all` is supported on
undeploy/monitor-undeploy for the daily teardown sweep.

Subcommands:
  trigger           Fire the Jenkins build and create state/<name>.json. Fast.
  wait              Poll until the recorded build finishes. Long-running.
  verify            Confirm the topology is registered with Slicer.
  monitor           Watch deploy_status walk to 'deployed'.
  undeploy          POST Slicer's undeploy endpoint (one --name, or --all).
  monitor-undeploy  Watch deploy_status walk back to 'not_deployed'.
  delete            DELETE topology record from Slicer (one --name, or --all).
  list              Show every tracked topology and its current stage.
  latest            Print the most-recently triggered topology's run_name.
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

QUEUE_POLL_INTERVAL_S = 2
QUEUE_POLL_TIMEOUT_S = 120
BUILD_POLL_INTERVAL_S = 30
BUILD_POLL_TIMEOUT_S = 30 * 60
SLICER_POLL_INTERVAL_S = 20
SLICER_POLL_TIMEOUT_S = 10 * 60
MONITOR_POLL_INTERVAL_S = 120
MONITOR_POLL_TIMEOUT_S = 90 * 60

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

# Lifecycle timestamps recorded in state files, in chronological order.
LIFECYCLE_KEYS = [
    "triggered_at",
    "build_finished_at",
    "verified_at",
    "deploy_started_at",
    "deployed_at",
    "undeploy_requested_at",
    "undeploy_started_at",
    "undeployed_at",
    "deleted_at",
]


def now_iso() -> str:
    return dt.datetime.now(CET).isoformat(timespec="seconds")


def state_path(name: str) -> Path:
    return STATE_DIR / f"{name}.json"


def load_state(name: str) -> dict:
    path = state_path(name)
    if not path.exists():
        raise FileNotFoundError(f"no state file at {path} — run `trigger --name {name}` first")
    return json.loads(path.read_text())


def save_state(name: str, state: dict) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    state_path(name).write_text(json.dumps(state, indent=2) + "\n")


def all_state_files() -> list[Path]:
    if not STATE_DIR.exists():
        return []
    return sorted(p for p in STATE_DIR.glob("*.json"))


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


def slicer_headers() -> dict[str, str]:
    return {"owner": SLICER_OWNER, "Content-Type": "application/json"}


def slicer_get(slicer_name: str) -> requests.Response:
    url = f"{SLICER_BASE}/v1_1/systest/{slicer_name}"
    params = [("field", f) for f in SLICER_FIELDS]
    return requests.get(url, headers=slicer_headers(), params=params, timeout=30, verify=False)


def extract_slicer_name(session: requests.Session, build_url: str) -> str | None:
    resp = session.get(f"{build_url}consoleText", timeout=60)
    resp.raise_for_status()
    m = SLICER_NAME_RE.search(resp.text)
    return m.group(1) if m else None


def default_run_name(now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now(CET)
    return f"Dispo-{now.strftime('%m%d')}"


def cmd_trigger(args: argparse.Namespace) -> int:
    name = args.name or default_run_name()
    path = state_path(name)
    if path.exists():
        if args.name:
            print(f"ERROR: {path} already exists. Pick a different --name or remove it first.", file=sys.stderr)
        else:
            print(
                f"ERROR: {path} already exists (today's default name is taken). "
                f"Pass --name <run_name> to provision another topology today.",
                file=sys.stderr,
            )
        return 2

    session = make_session(args.user, args.token)
    params = {**DEFAULT_PARAMS, "RUN_NAME": name}
    if args.version:
        params["RUN_FROM_BRANCH"] = args.version
    if args.ptest:
        params["PTEST_NAME"] = args.ptest
    if args.suite_args:
        params["SUITE_ARGS"] = args.suite_args

    resp = session.post(
        f"{JENKINS_BASE}{JOB_PATH}/buildWithParameters",
        params=params,
        headers=crumb_headers(session),
        timeout=30,
        allow_redirects=False,
    )
    resp.raise_for_status()
    queue_url = resp.headers["Location"]

    suite_note = f", SUITE_ARGS={params['SUITE_ARGS']!r}" if params["SUITE_ARGS"] else ""
    print(f"Triggered {name} (RUN_FROM_BRANCH={params['RUN_FROM_BRANCH']}, PTEST_NAME={params['PTEST_NAME']}{suite_note}), resolving build number...")
    build_number, build_url = resolve_queue_item(session, queue_url)

    save_state(name, {
        "name": name,
        "version": params["RUN_FROM_BRANCH"],
        "ptest": params["PTEST_NAME"],
        "suite_args": params["SUITE_ARGS"],
        "build_number": build_number,
        "build_url": build_url,
        "triggered_at": now_iso(),
    })

    print(f"Build #{build_number} started: {build_url}")
    print()
    print("Next:")
    print(f"  python3 trigger_topology.py wait    --name {name}")
    print(f"  python3 trigger_topology.py verify  --name {name}")
    print(f"  python3 trigger_topology.py monitor --name {name}")
    return 0


def cmd_wait(args: argparse.Namespace) -> int:
    state = load_state(args.name)
    name = state["name"]
    build_url = state["build_url"]
    build_number = state["build_number"]

    session = make_session(args.user, args.token)
    print(f"Waiting on {name} (build #{build_number}): {build_url}", flush=True)
    deadline = time.monotonic() + BUILD_POLL_TIMEOUT_S
    started = time.monotonic()
    while time.monotonic() < deadline:
        try:
            resp = session.get(f"{build_url}api/json", timeout=30)
            resp.raise_for_status()
        except requests.HTTPError as e:
            if e.response.status_code == 404:
                print(f"WARN: Jenkins build no longer available (404). Skipping extraction.", file=sys.stderr, flush=True)
                if state.get("slicer_name"):
                    print(f"Using slicer_name from prior run: {state['slicer_name']}", flush=True)
                    return 0
                print(f"ERROR: build log expired and no slicer_name recorded. Re-run `trigger` to start fresh.", file=sys.stderr, flush=True)
                return 2
            raise
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
                    state["build_finished_at"] = now_iso()
                    save_state(name, state)
                else:
                    print("WARN: could not find topology name in console log", file=sys.stderr, flush=True)
            return 0 if result == "SUCCESS" else 1
        elapsed = int(time.monotonic() - started)
        print(f"  building... ({elapsed // 60}m {elapsed % 60}s elapsed)", flush=True)
        time.sleep(BUILD_POLL_INTERVAL_S)
    print(f"ERROR: build did not finish within {BUILD_POLL_TIMEOUT_S // 60} minutes", file=sys.stderr, flush=True)
    return 2


def cmd_verify(args: argparse.Namespace) -> int:
    state = load_state(args.name)
    slicer_name = state.get("slicer_name")
    if not slicer_name:
        print(f"ERROR: {args.name}: no slicer_name in state — run `wait --name {args.name}` first", file=sys.stderr)
        return 2

    print(f"Verifying topology {slicer_name}", flush=True)
    deadline = time.monotonic() + SLICER_POLL_TIMEOUT_S
    started = time.monotonic()
    consecutive_404s = 0
    while time.monotonic() < deadline:
        resp = slicer_get(slicer_name)
        if resp.status_code == 404:
            consecutive_404s += 1
            if consecutive_404s >= 2:
                print(f"ERROR: topology {slicer_name} no longer exists in Slicer (TTL expired).", file=sys.stderr, flush=True)
                print(f"       Topologies auto-delete after a retention period. Re-run `trigger` to provision a fresh one.", file=sys.stderr, flush=True)
                return 2
            elapsed = int(time.monotonic() - started)
            print(f"  not in Slicer yet... ({elapsed // 60}m {elapsed % 60}s elapsed)", flush=True)
            time.sleep(SLICER_POLL_INTERVAL_S)
            continue
        consecutive_404s = 0
        resp.raise_for_status()
        data = resp.json()
        status = (data.get("deploy_status") or "").lower()
        if status:
            print(f"Verified: {data['name']} present in Slicer (deploy_status={status})", flush=True)
            if args.verbose:
                print(json.dumps(data, indent=2), flush=True)
            state["slicer_name"] = data["name"]
            state["verified_at"] = now_iso()
            save_state(args.name, state)
            return 0
        print(f"  empty deploy_status, retrying...", flush=True)
        time.sleep(SLICER_POLL_INTERVAL_S)
    print(f"ERROR: topology not verified within {SLICER_POLL_TIMEOUT_S // 60} minutes", file=sys.stderr, flush=True)
    return 2


def cmd_monitor(args: argparse.Namespace) -> int:
    state = load_state(args.name)
    slicer_name = state.get("slicer_name")
    if not slicer_name:
        print(f"ERROR: {args.name}: no slicer_name in state — run `wait --name {args.name}` first", file=sys.stderr)
        return 2

    print(f"Monitoring deploy_status of {slicer_name}", flush=True)
    started = time.monotonic()
    last_change = started
    last_status: str | None = None
    while time.monotonic() - last_change < MONITOR_POLL_TIMEOUT_S:
        resp = slicer_get(slicer_name)
        if resp.status_code == 404:
            print(f"ERROR: topology {slicer_name} no longer exists in Slicer (TTL expired).", file=sys.stderr, flush=True)
            print(f"       Topologies auto-delete after a retention period.", file=sys.stderr, flush=True)
            return 2
        resp.raise_for_status()
        data = resp.json()
        status = (data.get("deploy_status") or "").lower()
        elapsed = int(time.monotonic() - started)
        if status != last_status:
            print(f"  [{elapsed // 60}m {elapsed % 60}s] deploy_status -> {status!r}", flush=True)
            record_deploy_transition(args.name, status)
            last_status = status
            last_change = time.monotonic()
        else:
            print(f"  {status}... ({elapsed // 60}m {elapsed % 60}s elapsed)", flush=True)
        if status == "deploy_failed":
            print(f"ERROR: deployment failed — topology cannot recover from this state.", file=sys.stderr, flush=True)
            return 2
        if status == "deployed":
            print(f"Deployed in {elapsed // 60}m {elapsed % 60}s", flush=True)
            return 0
        time.sleep(MONITOR_POLL_INTERVAL_S)
    print(f"ERROR: deploy_status stuck at {last_status!r} for {MONITOR_POLL_TIMEOUT_S // 60} minutes", file=sys.stderr, flush=True)
    return 2


def record_deploy_transition(name: str, status: str) -> None:
    key = {
        "deploy_in_progress": "deploy_started_at",
        "deployed": "deployed_at",
    }.get(status)
    if not key:
        return
    try:
        state = load_state(name)
    except FileNotFoundError:
        return
    if key not in state:
        state[key] = now_iso()
        save_state(name, state)


def cmd_undeploy(args: argparse.Namespace) -> int:
    if args.all:
        names = [p.stem for p in all_state_files()]
        if not names:
            print("Nothing to undeploy — no state files found.")
            return 0
        print(f"Sweeping undeploy across {len(names)} state file(s)", flush=True)
        rc = 0
        for n in names:
            r = undeploy_one(n, skip_if_already_requested=True)
            if r != 0:
                rc = r
        return rc
    return undeploy_one(args.name, skip_if_already_requested=False)


def undeploy_one(name: str, skip_if_already_requested: bool) -> int:
    try:
        state = load_state(name)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    slicer_name = state.get("slicer_name")
    if not slicer_name:
        print(f"  {name}: no slicer_name yet (build still running?) — skipping", flush=True)
        return 0 if skip_if_already_requested else 2
    if state.get("undeployed_at"):
        print(f"  {name}: already undeployed at {state['undeployed_at']} — skipping", flush=True)
        return 0
    if skip_if_already_requested and state.get("undeploy_requested_at"):
        print(f"  {name}: undeploy already requested at {state['undeploy_requested_at']} — skipping", flush=True)
        return 0

    url = f"{SLICER_BASE}/v1_1/systest/{slicer_name}/undeploy"
    resp = requests.post(url, headers=slicer_headers(), json={"timeout": 300}, timeout=30, verify=False)
    if resp.status_code == 412:
        print(f"  {name}: topology no longer exists in Slicer (already deleted or expired) — skipping", flush=True)
        state["undeploy_requested_at"] = now_iso()
        state["undeployed_at"] = now_iso()
        save_state(name, state)
        return 0
    if resp.status_code not in (200, 202):
        print(f"ERROR: {name}: undeploy returned HTTP {resp.status_code}: {resp.text[:200]}", file=sys.stderr, flush=True)
        return 1
    state["undeploy_requested_at"] = now_iso()
    save_state(name, state)
    print(f"  {name}: undeploy accepted ({resp.status_code}) for {slicer_name}", flush=True)
    return 0


def cmd_monitor_undeploy(args: argparse.Namespace) -> int:
    if args.all:
        names = []
        for p in all_state_files():
            try:
                st = json.loads(p.read_text())
            except json.JSONDecodeError:
                continue
            if st.get("undeploy_requested_at") and not st.get("undeployed_at"):
                names.append(p.stem)
        if not names:
            print("Nothing to monitor — no topologies awaiting teardown.")
            return 0
    else:
        names = [args.name]

    targets: dict[str, str] = {}
    for n in names:
        try:
            st = load_state(n)
        except FileNotFoundError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        sn = st.get("slicer_name")
        if not sn:
            print(f"  {n}: no slicer_name — skipping", flush=True)
            continue
        targets[n] = sn

    if not targets:
        return 0

    print(f"Monitoring undeploy of {len(targets)} topology(ies): {', '.join(sorted(targets))}", flush=True)
    started = time.monotonic()
    last_change = {n: started for n in targets}
    last_status: dict[str, str | None] = {n: None for n in targets}
    pending = set(targets)
    rc = 0
    while pending:
        cycle_start = time.monotonic()
        for n in list(pending):
            slicer_name = targets[n]
            resp = slicer_get(slicer_name)
            if resp.status_code == 404:
                elapsed = int(time.monotonic() - started)
                print(f"  [{elapsed // 60}m {elapsed % 60}s] {n}: 404 from Slicer — record gone", flush=True)
                _mark_undeployed(n)
                pending.discard(n)
                continue
            resp.raise_for_status()
            status = (resp.json().get("deploy_status") or "").lower()
            elapsed = int(time.monotonic() - started)
            if status != last_status[n]:
                print(f"  [{elapsed // 60}m {elapsed % 60}s] {n}: deploy_status -> {status!r}", flush=True)
                record_undeploy_transition(n, status)
                last_status[n] = status
                last_change[n] = time.monotonic()
            if status == "not_deployed":
                print(f"  {n}: undeployed in {elapsed // 60}m {elapsed % 60}s", flush=True)
                pending.discard(n)
        for n in list(pending):
            if time.monotonic() - last_change[n] > MONITOR_POLL_TIMEOUT_S:
                print(f"ERROR: {n}: deploy_status stuck at {last_status[n]!r} for {MONITOR_POLL_TIMEOUT_S // 60} minutes", file=sys.stderr, flush=True)
                pending.discard(n)
                rc = 2
        if pending:
            elapsed_cycle = time.monotonic() - cycle_start
            time.sleep(max(0, MONITOR_POLL_INTERVAL_S - elapsed_cycle))
    return rc


def record_undeploy_transition(name: str, status: str) -> None:
    key = {
        "undeploy_in_progress": "undeploy_started_at",
        "not_deployed": "undeployed_at",
    }.get(status)
    if not key:
        return
    try:
        state = load_state(name)
    except FileNotFoundError:
        return
    # `not_deployed` is also the initial state — only record undeployed_at if
    # teardown was actually requested.
    if key == "undeployed_at" and not state.get("undeploy_requested_at"):
        return
    if key not in state:
        state[key] = now_iso()
        save_state(name, state)


def _mark_undeployed(name: str) -> None:
    try:
        state = load_state(name)
    except FileNotFoundError:
        return
    if "undeployed_at" not in state:
        state["undeployed_at"] = now_iso()
        save_state(name, state)


def cmd_list(args: argparse.Namespace) -> int:
    files = all_state_files()
    if not files:
        print("No tracked topologies.")
        return 0
    header = f"{'NAME':<22} {'VERSION':<18} {'PTEST':<22} {'SLICER_NAME':<60} {'STATUS':<22} LAST_CHECKED"
    print(header)
    for p in files:
        try:
            st = json.loads(p.read_text())
        except json.JSONDecodeError:
            print(f"{p.stem:<22} (corrupt state file)")
            continue
        slicer_name = st.get('slicer_name')
        name = st.get('name', p.stem)
        version = st.get('version', '-')
        ptest = st.get('ptest', '-')

        # Query Slicer for actual current status
        if slicer_name:
            resp = slicer_get(slicer_name)
            if resp.status_code == 404:
                status = "not_found_in_slicer"
                last_checked = now_iso()
            elif resp.ok:
                data = resp.json()
                status = (data.get("deploy_status") or "unknown").lower()
                last_checked = now_iso()
            else:
                status = f"error_{resp.status_code}"
                last_checked = now_iso()
        else:
            status = "not_created_yet"
            last_checked = st.get("triggered_at", "-")

        print(f"{name:<22} {version:<18} {ptest:<22} {(slicer_name or '-'):<60} {status:<22} {last_checked}")
    return 0


def current_stage(state: dict) -> tuple[str, str]:
    # Map to actual Slicer deploy_status states + pre-deployment stages
    if state.get("deleted_at"):
        return "deleted", state["deleted_at"]
    if state.get("undeployed_at"):
        return "not_deployed", state["undeployed_at"]
    if state.get("undeploy_started_at"):
        return "undeploy_in_progress", state["undeploy_started_at"]
    if state.get("deployed_at"):
        return "deployed", state["deployed_at"]
    if state.get("deploy_started_at"):
        return "deploy_in_progress", state["deploy_started_at"]
    if state.get("verified_at"):
        return "not_deployed", state["verified_at"]  # Topology exists but hasn't started deploying
    if state.get("build_finished_at"):
        return "not_deployed", state["build_finished_at"]  # Topology created but not yet verified
    if state.get("triggered_at"):
        return "triggered", state["triggered_at"]  # Build still running
    return "unknown", "-"


def cmd_delete(args: argparse.Namespace) -> int:
    if args.all:
        names = [p.stem for p in all_state_files()]
        if not names:
            print("Nothing to delete — no state files found.")
            return 0
        print(f"Sweeping delete across {len(names)} state file(s)", flush=True)
        skipped = []
        for n in names:
            was_skipped = delete_one(n, skip_if_already_deleted=True)
            if was_skipped:
                skipped.append(n)
        if skipped:
            print(f"Note: {len(skipped)} topology(ies) not ready yet (resources releasing): {', '.join(skipped)}", file=sys.stderr, flush=True)
            print("Retry delete in a moment: python3 trigger_topology.py delete --all", file=sys.stderr, flush=True)
        return 0
    return delete_one(args.name, skip_if_already_deleted=False)


def is_reservation_released(slicer_name: str) -> bool:
    resp = slicer_get(slicer_name)
    if resp.status_code == 404:
        return True
    if resp.ok:
        data = resp.json()
        status = data.get("reservation", {}).get("status", "").lower()
        return status != "reserved"
    return False


def request_release_resources(slicer_name: str) -> int:
    url = f"{SLICER_BASE}/v1_1/systest/{slicer_name}/release-resources"
    resp = requests.post(url, headers=slicer_headers(), json={"is_async": False}, timeout=30, verify=False)
    return resp.status_code


def wait_for_reservation_release(slicer_name: str, timeout_s: int = 600) -> bool:
    start = time.time()
    while time.time() - start < timeout_s:
        if is_reservation_released(slicer_name):
            return True
        time.sleep(20)
    return False


def delete_one(name: str, skip_if_already_deleted: bool) -> bool | int:
    try:
        state = load_state(name)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2 if not skip_if_already_deleted else False
    slicer_name = state.get("slicer_name")
    if not slicer_name:
        print(f"  {name}: no slicer_name yet (build still running?) — skipping", flush=True)
        return False if skip_if_already_deleted else 2
    if state.get("deleted_at"):
        if skip_if_already_deleted:
            print(f"  {name}: already deleted at {state['deleted_at']} — skipping", flush=True)
            return False
        print(f"WARN: {name}: already deleted at {state['deleted_at']}, deleting again", flush=True)

    if not is_reservation_released(slicer_name):
        print(f"  {name}: reservation still active, releasing resources...", flush=True)
        rc = request_release_resources(slicer_name)
        if rc not in (200, 202, 204):
            msg = f"unreserve returned HTTP {rc}"
            if skip_if_already_deleted:
                print(f"  {name}: {msg} — skipping for now", flush=True)
                return True
            print(f"ERROR: {name}: {msg}", file=sys.stderr, flush=True)
            return 1
        print(f"  {name}: waiting for reservation release...", flush=True)
        if not wait_for_reservation_release(slicer_name):
            msg = "reservation did not release within 10 minutes"
            if skip_if_already_deleted:
                print(f"  {name}: {msg} — skipping for now", flush=True)
                return True
            print(f"ERROR: {name}: {msg}", file=sys.stderr, flush=True)
            return 1

    url = f"{SLICER_BASE}/v1_1/systest/{slicer_name}"
    resp = requests.delete(url, headers=slicer_headers(), timeout=30, verify=False)
    if resp.status_code == 404:
        print(f"  {name}: topology no longer exists in Slicer (already deleted or expired) — marking as deleted", flush=True)
        state["deleted_at"] = now_iso()
        save_state(name, state)
        return False
    if resp.status_code == 412:
        if skip_if_already_deleted:
            print(f"  {name}: delete precondition failed — skipping for now", flush=True)
            return True
        print(f"ERROR: {name}: cannot delete — {resp.text[:200]}", file=sys.stderr, flush=True)
        return 1
    if resp.status_code not in (200, 204):
        print(f"ERROR: {name}: delete returned HTTP {resp.status_code}: {resp.text[:200]}", file=sys.stderr, flush=True)
        return 1 if not skip_if_already_deleted else False
    state["deleted_at"] = now_iso()
    save_state(name, state)
    print(f"  {name}: deleted", flush=True)
    return False


def cmd_purge(args: argparse.Namespace) -> int:
    files = all_state_files()
    if not files:
        print("No state files to purge.")
        return 0

    to_delete = []
    to_keep = []

    # Check each topology against Slicer
    for p in files:
        try:
            st = json.loads(p.read_text())
        except json.JSONDecodeError:
            to_delete.append((p, "corrupt state file"))
            continue

        slicer_name = st.get("slicer_name")
        if not slicer_name:
            to_keep.append((p, "not yet created in Slicer"))
            continue

        # Query Slicer
        resp = slicer_get(slicer_name)
        if resp.status_code == 404:
            to_delete.append((p, "no longer exists in Slicer"))
        else:
            to_keep.append((p, f"still in Slicer (status: {resp.json().get('deploy_status', '?')})"))

    if not to_delete:
        print("No expired topologies to purge. All state files are still valid.")
        return 0

    print(f"Will delete {len(to_delete)} expired state file(s):")
    for p, reason in to_delete:
        print(f"  {p.name} — {reason}")

    if to_keep:
        print(f"\nWill keep {len(to_keep)} active state file(s):")
        for p, reason in to_keep:
            print(f"  {p.name} — {reason}")

    if not args.force:
        response = input("\nProceed with purge? (type 'yes' to confirm): ").strip().lower()
        if response != "yes":
            print("Cancelled.")
            return 0

    for p, _ in to_delete:
        p.unlink()
        print(f"Deleted {p.name}")

    print(f"\nPurged {len(to_delete)} expired state file(s).")
    return 0


def cmd_latest(args: argparse.Namespace) -> int:
    files = all_state_files()
    if not files:
        print("ERROR: no state files found — run `trigger` first", file=sys.stderr)
        return 2
    latest_name = None
    latest_slicer = None
    latest_time = None
    for p in files:
        try:
            st = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        triggered = st.get("triggered_at")
        if triggered and (latest_time is None or triggered > latest_time):
            latest_name = st.get("name") or p.stem
            latest_slicer = st.get("slicer_name")
            latest_time = triggered
    if latest_name:
        print(latest_name)
        return 0
    print("ERROR: no valid state files found", file=sys.stderr)
    return 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--user", default=os.environ.get("JENKINS_USER") or None)
    p.add_argument("--token", default=os.environ.get("JENKINS_TOKEN") or None)

    sub = p.add_subparsers(dest="cmd", required=True)

    trig = sub.add_parser("trigger", help="Fire the build and create state/<name>.json")
    trig.add_argument("--name", help="RUN_NAME and state filename (default: Dispo-<MMDD> in CET)")
    trig.add_argument(
        "--version",
        help="AOS version, sent as RUN_FROM_BRANCH (default: AOS_latest_OB). Example: --version AOS_6.1.0_OB",
    )
    trig.add_argument("--ptest", help="PTEST_NAME override (default: evpn_mlag.vex)")
    trig.add_argument(
        "--suite-args",
        help='SUITE_ARGS passed verbatim to Jenkins. Example: --suite-args "--leaf3_os_type=vevo --leaf4_os_type=vevo"',
    )
    trig.set_defaults(func=cmd_trigger)

    wait = sub.add_parser("wait", help="Poll until the recorded build finishes")
    wait.add_argument("--name", required=True)
    wait.set_defaults(func=cmd_wait)

    verify = sub.add_parser("verify", help="Confirm topology is registered with Slicer")
    verify.add_argument("--name", required=True)
    verify.add_argument("--verbose", "-v", action="store_true", help="Also print the full Slicer payload (large)")
    verify.set_defaults(func=cmd_verify)

    monitor = sub.add_parser("monitor", help="Poll deploy_status until 'deployed'")
    monitor.add_argument("--name", required=True)
    monitor.set_defaults(func=cmd_monitor)

    undeploy = sub.add_parser("undeploy", help="Send Slicer's undeploy call")
    g = undeploy.add_mutually_exclusive_group(required=True)
    g.add_argument("--name", help="Undeploy a single topology by run_name")
    g.add_argument("--all", action="store_true", help="Sweep every state file with slicer_name and no undeploy_requested_at")
    undeploy.set_defaults(func=cmd_undeploy)

    monu = sub.add_parser("monitor-undeploy", help="Poll deploy_status until 'not_deployed'")
    g2 = monu.add_mutually_exclusive_group(required=True)
    g2.add_argument("--name")
    g2.add_argument("--all", action="store_true", help="Watch every state file with undeploy_requested_at and no undeployed_at")
    monu.set_defaults(func=cmd_monitor_undeploy)

    delete = sub.add_parser("delete", help="Delete topology record from Slicer")
    g3 = delete.add_mutually_exclusive_group(required=True)
    g3.add_argument("--name", help="Delete a single topology by run_name")
    g3.add_argument("--all", action="store_true", help="Sweep every state file with slicer_name and no deleted_at")
    delete.set_defaults(func=cmd_delete)

    lst = sub.add_parser("list", help="Show every tracked topology and its current stage")
    lst.set_defaults(func=cmd_list)

    lat = sub.add_parser("latest", help="Print the most-recently triggered topology's run_name")
    lat.set_defaults(func=cmd_latest)

    purge = sub.add_parser("purge", help="Delete all state files (start fresh)")
    purge.add_argument("--force", action="store_true", help="Skip confirmation prompt")
    purge.set_defaults(func=cmd_purge)

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
    except (RuntimeError, TimeoutError, FileNotFoundError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
