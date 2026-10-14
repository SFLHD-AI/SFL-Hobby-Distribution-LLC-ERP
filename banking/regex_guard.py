"""
Guard against user-written regex rules that could hang imports.

Python's ``re`` has no timeout, so a pattern like ``^(\w+\s?)*$`` on a long
description can take seconds per row and stall every import and sync. We
refuse patterns with nested quantifiers and time-box a probe against a
worst-case string when the rule is saved.
"""
import re
import threading
import time

_NESTED = re.compile(r"\((?:[^()\\]|\\.)*[*+}](?:[^()\\]|\\.)*\)\s*[*+{]")
_PROBE = ("ACH PAYMENT " * 20 + "1234567890 " * 6)[:300]


def nested_quantifier(pattern):
    """True when a quantified group itself contains a quantifier: (a+)+, (\w+\s?)*."""
    return bool(_NESTED.search(pattern or ""))


def compile_safe(pattern):
    """Return a compiled regex, or None if the pattern is invalid or unsafe."""
    if not pattern or len(pattern) > 200 or nested_quantifier(pattern):
        return None
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error:
        return None


def probe_duration(pattern, limit=0.25):
    """Seconds a search against a worst-case string takes, capped by ``limit``
    (the thread keeps running but the caller stops waiting). None = invalid."""
    rx = compile_safe(pattern)
    if rx is None:
        return None
    result = {}

    def run():
        t0 = time.monotonic()
        rx.search(_PROBE)
        result["t"] = time.monotonic() - t0

    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(limit)
    return result.get("t", limit)


def is_safe(pattern, limit=0.25):
    d = probe_duration(pattern, limit)
    return d is not None and d < limit
