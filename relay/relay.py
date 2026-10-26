#!/usr/bin/env python3
"""Codex <-> Claude relay: watch quota, pass the baton, resume after reset.

The policy this implements, in order:
  1. The baton holder works normally.
  2. At `handoff_threshold_pct` remaining it hands the baton to the healthiest
     idle agent -- but does not stop. It moves to `draining`: it spends the
     last sliver finishing the current step and writing the handoff note.
  3. When it finally hits the limit it goes `blocked` with a reset time.
  4. At reset it comes back `ready` and can take the baton again.

Subcommands:
  status   one-shot usage table
  run      the loop (add --once for a single tick, --dry-run to act on nothing)
  init     write config.json from the example
"""
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

RELAY_DIR = Path(__file__).resolve().parent
CONFIG_PATH = RELAY_DIR / "config.json"
EXAMPLE_PATH = RELAY_DIR / "config.example.json"
STATUS_PATH = RELAY_DIR / "status.json"
USAGE_PATH = RELAY_DIR / "usage.json"
HANDOFF_DIR = RELAY_DIR / "handoff"
LOG_PATH = RELAY_DIR / "relay.log"


def log(message: str) -> None:
    line = "[%s] %s" % (core.iso(core.now()), message)
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def load_config(path: Path) -> dict:
    config = core.read_json(path)
    if config is None:
        raise SystemExit("no config at %s -- run: python3 relay.py init" % path)
    config.setdefault("handoff_threshold_pct", 2.0)
    config.setdefault("poll_seconds", 60)
    config.setdefault("autostart", False)
    config.setdefault("order", sorted(config.get("agents", {})))
    return config


def blank_status(config: dict) -> dict:
    return {
        "version": 1,
        "updated_at": core.iso(core.now()),
        "project": config.get("project", "unnamed"),
        "holder": None,
        "phase": "idle",
        "task": config.get("task", {"title": "", "goal": "", "repo": "", "branch": ""}),
        "progress": {"done": [], "next": [], "notes": ""},
        "agents": {name: {"state": core.READY, "remaining_pct": None, "reset_at": None}
                   for name in config.get("agents", {})},
        "history": [],
    }


# --- probing ----------------------------------------------------------------

def probe_all(config: dict) -> dict:
    readings = {}
    for name, agent in config.get("agents", {}).items():
        spec = agent.get("probe")
        if not spec:
            readings[name] = {"ok": False, "error": "no probe configured"}
            continue
        settings = agent.get("settings", {})
        if isinstance(spec, str):
            command = "%s --settings %s" % (spec, shlex.quote(json.dumps(settings)))
        else:
            command = list(spec) + ["--settings", json.dumps(settings)]
        reading = core.run_probe(command, cwd=RELAY_DIR, timeout=agent.get("timeout", 90))
        reading.setdefault("agent", name)
        readings[name] = reading
    return readings


# --- state machine ----------------------------------------------------------

def reconcile(config: dict, status: dict, readings: dict) -> list:
    """Fold this tick's readings into the baton state. Returns events."""
    threshold = float(config["handoff_threshold_pct"])
    events = []
    agents = status.setdefault("agents", {})

    for name in config.get("agents", {}):
        entry = agents.setdefault(name, {"state": core.READY, "remaining_pct": None, "reset_at": None})
        reading = readings.get(name, {})
        was = entry.get("state", core.READY)

        if not reading.get("ok"):
            entry["error"] = reading.get("error", "probe failed")
            continue
        entry.pop("error", None)

        pct = reading.get("remaining_pct")
        entry["remaining_pct"] = pct
        entry["reset_at"] = reading.get("reset_at")
        entry["checked_at"] = core.iso(core.now())
        entry["confidence"] = reading.get("confidence")

        if reading.get("blocked"):
            state = core.BLOCKED
        elif was == core.BLOCKED:
            # The probe no longer reports a limit: the window reset.
            state = core.READY
            events.append("%s recovered (quota reset)" % name)
        elif pct is not None and pct <= threshold:
            state = core.DRAINING
        elif status.get("holder") == name:
            state = core.WORKING
        else:
            state = core.READY

        if state != was:
            events.append("%s %s -> %s" % (name, was, state))
            if state == core.BLOCKED:
                events.append("%s blocked until %s" % (name, entry.get("reset_at") or "unknown"))
        entry["state"] = state

    return events


def pick_successor(config: dict, status: dict, exclude: str):
    """First ready agent in config order.

    Order is the preference, not health: a READY agent is already above the
    threshold (anything at or below it is DRAINING), so taking the first one
    cannot hand the baton to an agent that is about to run dry. Preferring
    the healthiest instead would make the pick swing tick to tick.
    """
    for name in config["order"]:
        if name == exclude:
            continue
        if status["agents"].get(name, {}).get("state") == core.READY:
            return name
    return None


def write_handoff_note(config: dict, status: dict, sender, receiver, reason: str) -> Path:
    HANDOFF_DIR.mkdir(parents=True, exist_ok=True)
    stamp = core.now().strftime("%Y%m%dT%H%M%SZ")
    path = HANDOFF_DIR / ("%s-%s-to-%s.md" % (stamp, sender or "none", receiver))
    task = status.get("task", {})
    progress = status.get("progress", {})

    def bullets(items):
        return "\n".join("- %s" % item for item in items) or "- (none recorded)"

    path.write_text(
        "# Handoff: %s -> %s\n\n"
        "- when: %s\n- reason: %s\n- project: %s\n\n"
        "## Task\n%s\n\n%s\n\n- repo: %s\n- branch: %s\n\n"
        "## Done\n%s\n\n## Next\n%s\n\n## Notes\n%s\n\n"
        "## Quota at handoff\n%s\n\n"
        "## Your job\n"
        "1. Read `relay/status.json` for live state.\n"
        "2. Continue from **Next**, smallest verifiable step first.\n"
        "3. Keep `progress.done` / `progress.next` current -- the relay copies them into the next handoff.\n"
        % (
            sender or "(none)", receiver, core.iso(core.now()), reason,
            status.get("project", ""),
            task.get("title", "(untitled)"), task.get("goal", ""),
            task.get("repo", ""), task.get("branch", ""),
            bullets(progress.get("done", [])), bullets(progress.get("next", [])),
            progress.get("notes", "") or "(none)",
            "\n".join(
                "- %s: %s%% (%s)" % (
                    name,
                    entry.get("remaining_pct"),
                    entry.get("state"),
                ) for name, entry in status.get("agents", {}).items()
            ),
        ),
        encoding="utf-8",
    )
    return path


def run_hook(config: dict, agent_name: str, key: str, note: Path, dry_run: bool) -> None:
    spec = (config["agents"].get(agent_name) or {}).get(key)
    if not spec:
        return
    command = (spec
               .replace("{handoff}", str(note))
               .replace("{status}", str(STATUS_PATH))
               .replace("{agent}", agent_name))
    if dry_run:
        log("DRY-RUN would exec [%s.%s]: %s" % (agent_name, key, command))
        return
    log("exec [%s.%s]: %s" % (agent_name, key, command))
    try:
        subprocess.Popen(command, shell=True, cwd=str(RELAY_DIR))
    except OSError as exc:
        log("hook failed: %s" % exc)


def tick(config: dict, dry_run: bool = False) -> dict:
    status = core.read_json(STATUS_PATH) or blank_status(config)
    for name in config.get("agents", {}):
        status.setdefault("agents", {}).setdefault(
            name, {"state": core.READY, "remaining_pct": None, "reset_at": None})

    readings = probe_all(config)
    core.write_json(USAGE_PATH, {"updated_at": core.iso(core.now()), "readings": readings})

    for event in reconcile(config, status, readings):
        log(event)

    holder = status.get("holder")
    agents = status["agents"]

    # A draining agent keeps working; it just no longer holds the baton.
    for name, entry in agents.items():
        entry["directive"] = (
            "finish the current step, then write handoff notes into status.json"
            if entry.get("state") == core.DRAINING else None)

    needs_baton = holder is None or agents.get(holder, {}).get("state") in (core.DRAINING, core.BLOCKED)
    if needs_baton:
        successor = pick_successor(config, status, exclude=holder)
        if successor:
            reason = ("%s is %s" % (holder, agents[holder]["state"])) if holder else "no holder yet"
            note = write_handoff_note(config, status, holder, successor, reason)
            status["history"].append({
                "at": core.iso(core.now()), "from": holder, "to": successor,
                "reason": reason, "note": str(note),
            })
            log("HANDOFF %s -> %s (%s); note: %s" % (holder or "(none)", successor, reason, note))
            status["holder"] = successor
            status["phase"] = "working"
            status.pop("resume_at", None)
            agents[successor]["state"] = core.WORKING
            if holder:
                run_hook(config, holder, "notify_cmd", note, dry_run)
            if config.get("autostart"):
                run_hook(config, successor, "resume_cmd", note, dry_run)
            else:
                log("autostart off -- start %s yourself; note is at %s" % (successor, note))
        else:
            status["phase"] = "waiting_reset"
            resets = [core.parse_time(e.get("reset_at")) for e in agents.values()]
            resets = [r for r in resets if r]
            if resets:
                soonest = min(resets)
                status["resume_at"] = core.iso(soonest)
                log("all agents exhausted; next reset %s (in %s)"
                    % (core.iso(soonest), core.human_delta((soonest - core.now()).total_seconds())))
            else:
                log("all agents exhausted; no reset time known")
    else:
        status["phase"] = "working"
        status.pop("resume_at", None)

    status["updated_at"] = core.iso(core.now())
    core.write_json(STATUS_PATH, status)
    return status


# --- cli --------------------------------------------------------------------

def render(status: dict, readings: dict) -> str:
    rows = ["agent      state     remaining  reset                 source",
            "-" * 68]
    for name, entry in status.get("agents", {}).items():
        pct = entry.get("remaining_pct")
        rows.append("%-10s %-9s %8s  %-20s  %s" % (
            name,
            entry.get("state", "?"),
            "--" if pct is None else ("%.1f%%" % pct),
            entry.get("reset_at") or "-",
            (readings.get(name) or {}).get("source") or (entry.get("error") or "-"),
        ))
    rows.append("")
    rows.append("holder: %s   phase: %s" % (status.get("holder") or "(none)", status.get("phase")))
    if status.get("resume_at"):
        left = core.parse_time(status["resume_at"]) - core.now()
        rows.append("resume at %s (in %s)" % (status["resume_at"], core.human_delta(left.total_seconds())))
    return "\n".join(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["status", "run", "init"], nargs="?", default="status")
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument("--once", action="store_true", help="run a single tick and exit")
    parser.add_argument("--dry-run", action="store_true", help="never launch an agent")
    args = parser.parse_args()

    if args.command == "init":
        if CONFIG_PATH.exists():
            print("config.json already exists; leaving it alone")
            return 0
        CONFIG_PATH.write_text(EXAMPLE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        print("wrote %s -- edit budgets and commands, then: python3 relay.py run" % CONFIG_PATH)
        return 0

    config = load_config(Path(args.config))

    if args.command == "status":
        status = tick(config, dry_run=True)
        print(render(status, (core.read_json(USAGE_PATH) or {}).get("readings", {})))
        return 0

    log("relay up: threshold %.1f%%, poll %ss, autostart=%s"
        % (config["handoff_threshold_pct"], config["poll_seconds"], config.get("autostart")))
    while True:
        try:
            status = tick(config, dry_run=args.dry_run)
        except Exception as exc:  # keep the daemon alive through a bad tick
            log("tick failed: %r" % exc)
            status = {}
        if args.once:
            print(render(status, (core.read_json(USAGE_PATH) or {}).get("readings", {})))
            return 0

        delay = float(config["poll_seconds"])
        if status.get("phase") == "waiting_reset" and status.get("resume_at"):
            resume = core.parse_time(status["resume_at"])
            if resume:
                # Sleep toward the reset, but keep waking so a manual top-up
                # or a second machine coming back is noticed promptly.
                delay = max(delay, min((resume - core.now()).total_seconds() + 5, delay * 10))
        time.sleep(max(5.0, delay))


if __name__ == "__main__":
    raise SystemExit(main())
