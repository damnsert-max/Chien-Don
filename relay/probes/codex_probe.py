#!/usr/bin/env python3
"""Estimate remaining Codex / ChatGPT quota from local Codex session logs.

Same probe contract as claude_probe.py: one JSON object on stdout.

Codex rollout files have changed shape across versions, so this walks the
records looking for anything usage-shaped rather than assuming one layout.
Counters whose key mentions "total" are treated as cumulative (max wins);
everything else is summed per event.
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

DEFAULT_GLOBS = [
    "~/.codex/sessions/**/*.jsonl",
    "~/.codex/history.jsonl",
    "~/.codex/log/*.log",
]
DEFAULT_WEIGHTS = {"input": 1.0, "output": 1.0, "cache_read": 0.1, "cache_write": 1.25}

# Records authored by you or returned by a tool are never a limit notice.
SKIP_TYPES = {"user", "message", "function_call", "function_call_output", "reasoning"}

_ALIASES = {
    "prompt_tokens": "input_tokens",
    "completion_tokens": "output_tokens",
    "cached_input_tokens": "cache_read_input_tokens",
    "cache_read_tokens": "cache_read_input_tokens",
    "reasoning_output_tokens": "output_tokens",
}
_USAGE_KEYS = ("input_tokens", "output_tokens", "prompt_tokens", "completion_tokens")


def usage_blocks(node, key=""):
    """Yield (key, usage-dict) pairs without descending into a match."""
    if isinstance(node, dict):
        if any(k in node for k in _USAGE_KEYS):
            yield key, node
            return
        for name, value in node.items():
            for found in usage_blocks(value, name):
                yield found
    elif isinstance(node, list):
        for value in node:
            for found in usage_blocks(value, key):
                yield found


def normalize(block):
    out = {}
    for name, value in block.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        out[_ALIASES.get(name, name)] = out.get(_ALIASES.get(name, name), 0) + value
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--settings", default="{}")
    args = parser.parse_args()

    try:
        settings = json.loads(args.settings) or {}
    except json.JSONDecodeError as exc:
        print(json.dumps({"ok": False, "agent": "codex", "error": "bad --settings: %s" % exc}))
        return 0

    patterns = settings.get("globs") or DEFAULT_GLOBS
    window_hours = float(settings.get("window_hours", 5))
    budget = float(settings.get("token_budget", 4_000_000))
    weights = dict(DEFAULT_WEIGHTS, **(settings.get("weights") or {}))

    cutoff = core.now() - timedelta(hours=window_hours)
    incremental = 0.0
    cumulative = 0.0
    events = 0
    files = 0
    limit = None

    for pattern in patterns:
        for path in sorted(glob.glob(str(core.expand(pattern)), recursive=True)):
            files += 1
            file_time = _mtime(path)
            if str(path).endswith(".log"):
                hit = _scan_text(path)
                if hit and (limit is None or _newer(hit, limit)):
                    limit = hit
                continue

            for record in core.iter_jsonl(path):
                stamp = core.parse_time(
                    record.get("timestamp") or record.get("ts") or record.get("created_at")
                ) or file_time

                if record.get("type") not in SKIP_TYPES:
                    hit = core.find_limit(record)
                    if hit:
                        hit["at"] = stamp
                        if limit is None or _newer(hit, limit):
                            limit = hit

                if stamp is None or stamp < cutoff:
                    continue
                for key, block in usage_blocks(record):
                    weighed = core.weigh(normalize(block), weights)
                    if not weighed:
                        continue
                    events += 1
                    if "total" in key.lower():
                        cumulative = max(cumulative, weighed)
                    else:
                        incremental += weighed

    used = max(incremental, cumulative)
    reset_at = limit.get("reset_at") if limit else None
    if not limit:
        blocked = False
    elif reset_at is not None:
        blocked = reset_at > core.now()
    else:
        at = limit.get("at")
        blocked = at is not None and at >= cutoff

    print(json.dumps({
        "ok": True,
        "agent": "codex",
        "remaining_pct": round(0.0 if blocked else core.remaining_pct(used, budget), 2),
        "blocked": blocked,
        "reset_at": core.iso(reset_at),
        "used_tokens": round(used),
        "budget_tokens": round(budget),
        "window_hours": window_hours,
        "messages": events,
        "files_scanned": files,
        "source": "codex-rollout",
        "confidence": "measured" if blocked else "estimate",
        "evidence": (limit or {}).get("evidence"),
    }, ensure_ascii=False))
    return 0


def _scan_text(path):
    best = None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                hit = core.find_limit({}, raw=line)
                if hit and (best is None or _newer(hit, best)):
                    hit.setdefault("at", _mtime(path))
                    best = hit
    except OSError:
        return None
    return best


def _mtime(path):
    try:
        return core.parse_time(Path(path).stat().st_mtime)
    except OSError:
        return None


def _newer(candidate, current) -> bool:
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
