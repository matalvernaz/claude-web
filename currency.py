"""Currency resolution + USD-rate caching for the cost UI.

Anthropic bills in USD and the SDK reports per-turn cost in USD, so USD is
the default and truthful display currency. An operator can opt into a local
approximation by setting ``$CLAUDE_WEB_CURRENCY`` to an ISO-4217 code, but
that figure is an ECB-rate estimate that won't match a card issuer's actual
conversion, so it stays strictly opt-in.

Two pieces:

* ``resolve_currency(override)`` — honor an explicit ISO-4217 override, else
  USD. No locale sniffing: the billing currency doesn't depend on the
  caller's browser language.
* ``usd_rate(currency)`` — return the conversion rate (USD * rate = local),
  fetched daily from frankfurter.app (no API key, ECB rates) and cached on
  disk so a flaky network doesn't blank out the cost UI.

USD always returns rate 1.0 without any network call. If the rate fetch
fails and there's no cached value, the caller gets None and is expected
to display USD instead.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

import httpx

log = logging.getLogger("claude-web.currency")


def resolve_currency(override: Optional[str] = None) -> str:
    """Return the ISO-4217 code to display costs in.

    Defaults to USD — the currency Anthropic actually bills in. ``override``
    (typically ``$CLAUDE_WEB_CURRENCY``) pins a local currency for operators
    who want an approximate local figure; otherwise costs stay in USD.
    """
    if override:
        code = override.strip().upper()[:3]
        if len(code) == 3 and code.isalpha():
            return code
    return "USD"


# ─── Rate cache ───────────────────────────────────────────────────────────────

_RATE_TTL_SECONDS = 6 * 3600  # frankfurter refreshes ECB rates daily
_FETCH_TIMEOUT_SECONDS = 5.0
# frankfurter.app 301s to frankfurter.dev/v1/ as of late 2025; hit the new
# endpoint directly to avoid an extra redirect hop per fetch.
_FRANKFURTER_URL = "https://api.frankfurter.dev/v1/latest"

_lock = threading.Lock()
_cache: dict[str, tuple[float, float]] = {}  # currency → (fetched_at, rate)
_cache_path: Optional[Path] = None


def configure_cache(path: Path) -> None:
    """Point the on-disk rate cache at ``path``. Idempotent.

    Called once during app startup so the cache survives restarts — without
    it, every boot would trigger a fresh frankfurter.app round-trip on the
    first ``/api/usage`` request.
    """
    global _cache_path
    _cache_path = path
    _load_cache_from_disk()


def _load_cache_from_disk() -> None:
    if _cache_path is None or not _cache_path.exists():
        return
    try:
        text = _cache_path.read_text(encoding="utf-8").strip()
        if not text:
            return
        data = json.loads(text)
    except (OSError, ValueError):
        log.warning("currency rate cache at %s is unreadable; ignoring", _cache_path)
        return
    rates = data.get("rates", {}) if isinstance(data, dict) else {}
    with _lock:
        for code, entry in rates.items():
            if not isinstance(entry, dict):
                continue
            try:
                rate = float(entry["rate"])
                if not (rate > 0 and rate < float("inf")):  # reject 0/neg/NaN/inf
                    continue
                _cache[code.upper()] = (float(entry["fetched_at"]), rate)
            except (KeyError, TypeError, ValueError):
                continue


def _save_cache_to_disk() -> None:
    if _cache_path is None:
        return
    payload = {
        "rates": {
            code: {"fetched_at": ts, "rate": rate}
            for code, (ts, rate) in _cache.items()
        }
    }
    try:
        tmp = _cache_path.with_suffix(_cache_path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, _cache_path)
    except OSError:
        log.exception("currency rate cache write failed")


def usd_rate(currency: str) -> Optional[float]:
    """Return how many ``currency`` units one USD buys.

    USD short-circuits to 1.0. Cache hits within TTL skip the network.
    Network failure returns stale cache if available, else ``None``.
    """
    code = (currency or "USD").upper()
    if code == "USD":
        return 1.0

    now = time.time()
    with _lock:
        cached = _cache.get(code)
    if cached and (now - cached[0]) < _RATE_TTL_SECONDS:
        return cached[1]

    try:
        with httpx.Client(timeout=_FETCH_TIMEOUT_SECONDS) as client:
            resp = client.get(_FRANKFURTER_URL, params={"from": "USD", "to": code})
            resp.raise_for_status()
            data = resp.json()
        rate = float(data["rates"][code])
        if not (rate > 0 and rate < float("inf")):  # reject 0/neg/NaN/inf
            raise ValueError(f"implausible rate {rate!r}")
    except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
        log.warning("frankfurter rate fetch for %s failed: %s", code, exc)
        # Serve stale rather than blanking the cost UI.
        return cached[1] if cached else None

    with _lock:
        _cache[code] = (now, rate)
        _save_cache_to_disk()
    return rate
