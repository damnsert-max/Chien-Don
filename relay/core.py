"""Shared helpers for the Codex <-> Claude usage relay.

Stdlib only, Python 3.9+, so it runs on a stock macOS python3.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

RELAY_DIR = Path(__file__).resolve().parent

# Agent lifecycle states.
READY = "ready"        # has quota, idle
WORKING = "working"    # has quota, holds the baton
DRAINING = "draining"  # past the handoff threshold, burning the remainder
BLOCKED = "blocked"    # out of quota, waiting for reset


def now() -> datetime:
    return datetime.now(timezone.utc)


def local_tz():
    return datetime.now().astimezone().tzinfo


def iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


_ORDINAL = re.compile(r"(\d{1,2})(st|nd|rd|th)\b", re.IGNORECASE)

_HUMAN_FORMATS = (
    "%b %d %Y %I:%M %p",
    "%B %d %Y %I:%M %p",
    "%b %d %Y %H:%M",
    "%B %d %Y %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
)


def parse_time(value: Any) -> Optional[datetime]:
    """Parse ISO-8601, an epoch number, or a human string.

    Handles the exact shape the limit banners use, e.g.
    "Oct 27th, 2026 2:16 AM". Naive results are read as local time.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        # Epoch seconds, or milliseconds if it is implausibly large.
        seconds = value / 1000.0 if value > 1e11 else float(value)
        try:
            return datetime.fromtimestamp(seconds, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    text = str(value).strip()
    if not text:
        return None

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=local_tz())
    except ValueError:
        pass

    cleaned = " ".join(_ORDINAL.sub(r"\1", text).replace(",", " ").split())
    for fmt in _HUMAN_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).replace(tzinfo=local_tz())
        except ValueError:
            continue
    return None


def human_delta(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return "%dh%02dm" % (hours, minutes)
    if minutes:
        return "%dm%02ds" % (minutes, secs)
    return "%ds" % secs


def read_json(path: Union[str, Path], default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: Union[str, Path], payload: Any) -> None:
    """Write atomically so a reader never sees a half-written baton."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".%s." % path.name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def iter_jsonl(path: Union[str, Path]):
    """Yield parsed records, skipping damaged lines (logs get truncated)."""
    try:
        handle = open(path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return
    with handle:
        for line in handle:
            line = line.strip()
            if not line or line[0] not in "{[":
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def expand(path: Union[str, Path]) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(str(path))))


def run_probe(spec: Union[str, Sequence[str]], cwd: Union[str, Path], timeout: int = 90) -> Dict[str, Any]:
    """Run a probe and return the last JSON object it printed.

    A probe is any executable that prints one JSON object on stdout. That
    keeps the contract language-agnostic: swap in a shell one-liner that
    hits a real usage endpoint and the relay needs no changes.
    """
    shell = isinstance(spec, str)
    cmd: Any = spec if shell else [str(part) for part in spec]
    try:
        proc = subprocess.run(
            cmd, shell=shell, cwd=str(cwd),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "probe timed out after %ss" % timeout}
    except OSError as exc:
        return {"ok": False, "error": "probe could not start: %s" % exc}

    for line in reversed((proc.stdout or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload.setdefault("ok", True)
        return payload

    detail = (proc.stderr or "").strip() or (proc.stdout or "").strip()
    return {"ok": False, "error": (detail[:400] or "probe exited %d with no JSON" % proc.returncode)}


# --- usage estimation -------------------------------------------------------

# Phrasings the CLIs use when quota runs out. The reset timestamp is the
# authoritative signal; the percentage below it is only an estimate.
_LIMIT_MARKERS = (
    "usage limit",
    "rate limit",
    "rate_limit",
    "out of credits",
    "quota exceeded",
    "exceeded your current quota",
)

_RESET_PATTERNS = (
    re.compile(r"try again at\s+([^.\n\"]+?)(?:\.|\"|$)", re.IGNORECASE),
    re.compile(r"resets? (?:at|on)\s+([^.\n\"]+?)(?:\.|\"|$)", re.IGNORECASE),
    re.compile(r"available again (?:at|on)\s+([^.\n\"]+?)(?:\.|\"|$)", re.IGNORECASE),
)

# Keys the CLIs use to carry a machine-readable reset time.
_RESET_KEYS = ("resetsAt", "resets_at", "resetAt", "reset_at", "retryAt", "retry_at", "unified_reset")


def find_limit(record: Dict[str, Any], raw: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Return {'reset_at': dt|None, 'evidence': str} if a record reports a limit."""
    text = raw if raw is not None else json.dumps(record, ensure_ascii=False)
    lowered = text.lower()
    if not any(marker in lowered for marker in _LIMIT_MARKERS):
        return None

    reset = None
    for key in _RESET_KEYS:
        match = re.search(r'"%s"\s*:\s*("?)([^",}]+)\1' % key, text)
        if match:
            reset = parse_time(match.group(2))
            if reset:
                break
    if reset is None:
        for pattern in _RESET_PATTERNS:
            match = pattern.search(text)
            if match:
                reset = parse_time(match.group(1))
                if reset:
                    break

    index = lowered.find("usage limit")
    if index < 0:
        index = max(0, lowered.find("rate limit"))
    return {"reset_at": reset, "evidence": text[max(0, index - 60):index + 180].strip()}


def weigh(usage: Dict[str, Any], weights: Dict[str, float]) -> float:
    """Collapse a usage block into one weighted token count.

    Cached reads are far cheaper than fresh input, so they are discounted
    rather than counted at par; the weights are configurable because the
    exact ratio is a billing detail, not something to hard-code.
    """
    return (
        float(usage.get("input_tokens") or 0) * weights.get("input", 1.0)
        + float(usage.get("output_tokens") or 0) * weights.get("output", 1.0)
        + float(usage.get("cache_read_input_tokens") or 0) * weights.get("cache_read", 0.1)
        + float(usage.get("cache_creation_input_tokens") or 0) * weights.get("cache_write", 1.25)
    )


def remaining_pct(used: float, budget: float) -> float:
    if budget <= 0:
        return 100.0
    return max(0.0, min(100.0, (1.0 - used / budget) * 100.0))
