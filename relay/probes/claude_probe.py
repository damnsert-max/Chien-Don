#!/usr/bin/env python3
"""Estimate remaining Claude Code quota from the local transcripts.

Prints one JSON object on stdout (the probe contract in core.run_probe).

Two signals, and they are not equally trustworthy:
  * blocked / reset_at comes from a real limit event in the log -- authoritative.
  * remaining_pct is an estimate: weighted tokens in a rolling window against
    a budget you configure. Anthropic does not publish a token number for
    subscription limits, so calibrate `token_budget` from your own runs.
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core  # noqa: E402

DEFAULT_GLOBS = ["~/.claude/projects/*/*.jsonl"]

# Only the CLI's own notices can report a limit. User turns, assistant turns
# and tool results are excluded on purpose: a transcript that merely *talks
# about* usage limits must not read as being rate limited.
NOTICE_TYPES = {"system", "rate_limit_event", "result", "error"}
DEFAULT_WEIGHTS = {"input": 1.0, "output": 1.0, "cache_read": 0.1, "cache_write": 1.25}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--settings", default="{}", help="JSON object of probe settings")
    args = parser.parse_args()

    try:
        settings = json.loads(args.settings) or {}
    except json.JSONDecodeError as exc:
        print(json.dumps({"ok": False, "agent": "claude", "error": "bad --settings: %s" % exc}))
        return 0

    patterns = settings.get("globs") or DEFAULT_GLOBS
    window_hours = float(settings.get("window_hours", 5))
    budget = float(settings.get("token_budget", 8_000_000))
    weights = dict(DEFAULT_WEIGHTS, **(settings.get("weights") or {}))

    cutoff = core.now() - timedelta(hours=window_hours)
    used = 0.0
    events = 0
    files = 0
    limit = None

    for pattern in patterns:
        for path in sorted(glob.glob(str(core.expand(pattern)), recursive=True)):
            files += 1
            for record in core.iter_jsonl(path):
                stamp = core.parse_time(record.get("timestamp"))

                if record.get("type") in NOTICE_TYPES:
                    hit = core.find_limit(record)
                    if hit:
                        hit["at"] = stamp
                        if limit is None or _newer(hit, limit):
                            limit = hit

                if record.get("type") != "assistant":
                    continue
                if stamp is None or stamp < cutoff:
                    continue
                usage = (record.get("message") or {}).get("usage")
                if not isinstance(usage, dict):
                    continue
                used += core.weigh(usage, weights)
                events += 1

    reset_at = limit.get("reset_at") if limit else None
    if not limit:
        blocked = False
    elif reset_at is not None:
        blocked = reset_at > core.now()
    else:
        # No parseable reset time: trust the notice only while it is recent,
        # otherwise a one-off limit would pin the agent as blocked forever.
        at = limit.get("at")
        blocked = at is not None and at >= cutoff

    print(json.dumps({
        "ok": True,
        "agent": "claude",
        "remaining_pct": round(0.0 if blocked else core.remaining_pct(used, budget), 2),
        "blocked": blocked,
        "reset_at": core.iso(reset_at),
        "used_tokens": round(used),
        "budget_tokens": round(budget),
        "window_hours": window_hours,
        "messages": events,
        "files_scanned": files,
        "source": "claude-transcript",
        "confidence": "measured" if blocked else "estimate",
        "evidence": (limit or {}).get("evidence"),
    }, ensure_ascii=False))
    return 0


def _newer(candidate, current) -> bool:
    """Prefer the notice with the latest reset, falling back to latest seen."""
    new, old = candidate.get("reset_at"), current.get("reset_at")
    if new is not None and old is not None:
        return new > old
    if new is not None:
        return True
    if old is not None:
        return False
    new_at, old_at = candidate.get("at"), current.get("at")
    return new_at is not None and (old_at is None or new_at >= old_at)


if __name__ == "__main__":
    raise SystemExit(main())
