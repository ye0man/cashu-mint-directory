#!/usr/bin/env python3
"""
Update docs/mints.json by discovering mint URLs from live sources, probing
each mint, normalizing /v1/info responses, scoring freshness, and preserving
manual contact details from the existing file.

Designed to run from GitHub Actions (ubuntu-latest) with Python 3.11+.
The only third-party dependency is `websockets`, used for NIP-87 discovery.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import re
import socket
import ssl
import sys
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, urlparse

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MINTS_JSON = PROJECT_ROOT / "docs" / "mints.json"
CANDIDATES_JSON = PROJECT_ROOT / "data" / "candidates.json"

# Community-submitted mints (one URL per line, '#' comments allowed). Entries
# are added by approved PRs from .github/workflows/mint-submission.yml.
MANUAL_MINTS = PROJECT_ROOT / "data" / "manual_mints.txt"

# Declarative discovery sources. Missing file falls back to DEFAULT_SOURCES.
SOURCES_JSON = PROJECT_ROOT / "data" / "sources.json"

DEFAULT_SOURCES: dict[str, Any] = {
    "manual": {"enabled": True},
    "mintradar": {
        "enabled": True,
        "endpoint": (
            "https://mintradar.org/api/v1/wallets/mints"
            "?includeOffline=true&includeTest=true&includeArchived=true"
        ),
    },
    "cashumints": {
        "enabled": True,
        "endpoint": "https://cashumints.space/api/mints",
        "types": ["cashu"],
    },
    "nip87": {
        "enabled": True,
        "kinds": [38172, 38000],
        "relays": [
            "wss://relay.damus.io",
            "wss://nos.lol",
            "wss://relay.primal.net",
            "wss://relay.cashumints.space",
            "wss://nostr.mintradar.org",
            "wss://relay.azzamo.net",
            "wss://nostr.oxtr.dev",
            "wss://offchain.pub",
        ],
        "timeout_seconds": 10,
    },
}

# Hosts that are indexers/junk rather than mints.
DENY_HOSTS = {
    "lnbits.com",
    "www.lnbits.com",
    "cashumints.space",
    "www.cashumints.space",
    "mintradar.org",
    "www.mintradar.org",
    "github.com",
    "localhost",
}
DENY_SUFFIXES = (".onion", ".local", ".invalid", ".example", ".test")

# Optional NUTs are everything except mandatory 0-6. We track up to 30 for now
# but the script is dynamic — it will include any NUT number it sees.
MANDATORY_NUTS = {0, 1, 2, 3, 4, 5, 6}
MAX_KNOWN_NUT = 30

# Known unit aliases. Anything not listed is kept as-is.
UNIT_ALIASES = {
    "sats": "sat",
    "satoshis": "sat",
    "btc": "btc",
    "bitcoin": "btc",
    "msats": "msat",
    "msatoshis": "msat",
    "usd": "usd",
    "us dollar": "usd",
    "eur": "eur",
    "euro": "eur",
    "gbp": "gbp",
    "cad": "cad",
    "aud": "aud",
    "chf": "chf",
    "jpy": "jpy",
}

# Fallback release table. The live GitHub Releases lookup supersedes these
# dates when it succeeds; this table keeps scoring sane when it does not.
IMPLEMENTATION_RELEASES: dict[str, dict[str, str]] = {
    "nutshell": {
        "0.22.1": "2026-11-03",
        "0.22.0": "2026-11-03",
        "0.21.0": "2026-09-16",
        "0.20.3": "2026-07-22",
        "0.20.2": "2026-07-03",
        "0.20.1": "2026-05-31",
        "0.20.0": "2026-03-31",
    },
    "cdk-mintd": {
        "0.18.3": "2026-10-29",
        "0.18.2": "2026-10-14",
        "0.18.1": "2026-09-18",
        "0.18.0": "2026-09-02",
        "0.17.7": "2026-09-16",
        "0.17.6": "2026-08-26",
    },
}

GITHUB_RELEASE_REPOS = {
    "nutshell": "cashubtc/nutshell",
    "cdk-mintd": "cashubtc/cdk",
}

# Weights for freshness score (0-100, higher is fresher).
WEIGHTS = {
    "online": 40,
    "nut_recency": 25,
    "version_age": 20,
    "contact": 10,
    "time_sync": 5,
}

VERSION_AGE_THRESHOLD_DAYS = 180
TIME_SYNC_TOLERANCE_SECONDS = 300

REQUEST_TIMEOUT = 20  # seconds
USER_AGENT = "cashu-mint-directory-bot/1.0 (+https://github.com/ye0man/cashu-mint-directory)"

HOST_RE = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}$")
PATH_RE = re.compile(r"^[A-Za-z0-9._~/-]*$")
PUBKEY_RE = re.compile(r"^0[23][0-9a-fA-F]{64}$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return now_utc().isoformat().replace("+00:00", "Z")


def http_get(url: str) -> tuple[int, Any | None]:
    """
    Fetch JSON from a URL. Returns (status_code, parsed_json_or_none).
    Non-2xx responses still return the status code but None for JSON.
    """
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json, */*",
        },
        method="GET",
    )
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT, context=ctx) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.getcode(), json.loads(body)
            except json.JSONDecodeError:
                return resp.getcode(), None
    except HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
            return e.code, json.loads(body)
        except Exception:
            return e.code, None
    except URLError:
        return 0, None
    except Exception:
        return 0, None


def normalize_mint_url(raw: str) -> str | None:
    """
    Normalize a mint URL: https only, lowercase host, optional port, no
    credentials/query/fragment, trailing slash and a trailing /v1/info
    stripped, path casing preserved.
    """
    s = (raw or "").strip().strip("<>`'\"")
    if not s:
        return None
    if not re.match(r"^[a-z][a-z0-9+.-]*://", s, re.I):
        s = "https://" + s
    try:
        parts = urlsplit(s)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme.lower() != "https":
        return None
    if parts.username or parts.password or parts.query or parts.fragment:
        return None
    host = (parts.hostname or "").lower()
    if not HOST_RE.match(host) or host.endswith(".onion"):
        return None
    path = re.sub(r"/+$", "", parts.path)
    path = re.sub(r"/v1/info$", "", path, flags=re.I)
    path = re.sub(r"/+$", "", path)
    if not PATH_RE.match(path):
        return None
    netloc = f"{host}:{port}" if port and port != 443 else host
    return f"https://{netloc}{path}"


def resolve_host(host: str) -> tuple[str, str]:
    """
    Classify a host as 'public', 'private', 'unresolved', or 'denied'.

    Unresolved hosts are kept for historical records (a dead mint is still a
    mint), but are never fetched: the probe gets an offline record instead.
    """
    hostname = (urlsplit("//" + host).hostname or host).lower()
    if hostname in DENY_HOSTS or hostname.endswith(DENY_SUFFIXES):
        return "denied", "is a denylisted or non-public host"
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        return "unresolved", f"DNS lookup failed ({exc.strerror or exc})"
    addrs = {info[4][0] for info in infos}
    for addr in addrs:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
        if not ip.is_global:
            return "private", f"resolves to a non-public address ({ip})"
    return "public", ""


def dedupe_key(url: str) -> str:
    """Case-insensitive identity for a mint URL (host + path + port)."""
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    netloc = f"{host}:{parsed.port}" if parsed.port else host
    return f"{netloc}{parsed.path.lower()}"


def host_is_public(host: str) -> tuple[bool, str]:
    """Resolve a hostname and refuse private, loopback, or otherwise non-global targets."""
    state, why = resolve_host(host)
    if state == "public":
        return True, ""
    return False, why


def host_is_admissible(host: str) -> tuple[bool, str]:
    """Discovery may keep unresolved hosts as offline history, but never private ones."""
    state, why = resolve_host(host)
    return state in ("public", "unresolved"), why


def is_test_mint(url: str, hint: bool | None = None) -> bool:
    if hint:
        return True
    host = (urlsplit(url).hostname or "").lower()
    return (
        "testnut" in host
        or host.startswith("test.")
        or host == "8333.space"
        or "rugs" in host
    )


def parse_version(version_field: str | None) -> tuple[str | None, str | None]:
    """
    Parse the NUT-06 version field, e.g. 'Nutshell/0.20.3' -> ('Nutshell', '0.20.3').
    """
    if not version_field:
        return None, None
    parts = str(version_field).split("/", 1)
    impl = parts[0].strip() if parts[0] else None
    version = parts[1].strip().strip("/") if len(parts) > 1 and parts[1] else None
    return impl, version


def normalize_implementation(name: str | None) -> str | None:
    if not name:
        return None
    n = name.strip().lower()
    if "nutshell" in n:
        return "Nutshell"
    if "cdk" in n and "mint" in n:
        return "cdk-mintd"
    if "nutmix" in n:
        return "Nutmix"
    if "lek" in n:
        return "LekMint"
    # Preserve original casing for unknown implementations.
    return name.strip()


def normalize_unit(unit: str | None) -> str | None:
    if not unit:
        return None
    key = unit.strip().lower()
    return UNIT_ALIASES.get(key, key)


def _parse_legacy_method(value: str) -> tuple[str, list[str]]:
    """Parse legacy 'method (unit)' strings into (method, [unit])."""
    match = re.match(r"^(.+?)\s*\(([^)]+)\)$", value.strip())
    if match:
        return match.group(1).strip(), [match.group(2).strip()]
    return value.strip(), []


def normalize_methods(methods: Any) -> list[dict[str, Any]]:
    """
    Convert legacy 'method (unit)' strings or partial structured dicts into
    the canonical format: [{'method': str, 'units': [str, ...]}, ...].
    """
    if not isinstance(methods, list):
        return []
    method_units: dict[str, set[str]] = {}
    for item in methods:
        if isinstance(item, dict):
            method = str(item.get("method") or "").strip()
            units = item.get("units") or []
            if not isinstance(units, list):
                units = [units]
        elif isinstance(item, str):
            method, units = _parse_legacy_method(item)
        else:
            continue
        if not method:
            continue
        for unit in units:
            unit = str(unit).strip()
            if unit:
                method_units.setdefault(method, set()).add(unit)
    return [{"method": m, "units": sorted(us)} for m, us in sorted(method_units.items())]


def parse_nuts(
    nuts_obj: dict[str, Any] | None,
) -> tuple[list[int], list[str], list[str], dict[str, set[str]], dict[str, set[str]], bool, bool]:
    """
    From the /v1/info 'nuts' object return:
      - optional_nuts: sorted list of supported optional NUT numbers
      - all_units: deduplicated list of supported units (from NUT-04/05 methods)
      - stale_reasons: list of reasons discovered while parsing
      - mint_methods: dict mapping NUT-04 method name -> set of supported units
      - melt_methods: dict mapping NUT-05 method name -> set of supported units
      - nut4_disabled: whether NUT-04 minting is disabled
      - nut5_disabled: whether NUT-05 melting is disabled
    """
    optional_nuts: list[int] = []
    units: list[str] = []
    stale_reasons: list[str] = []
    mint_methods: dict[str, set[str]] = {}
    melt_methods: dict[str, set[str]] = {}
    nut4_disabled = False
    nut5_disabled = False

    if not isinstance(nuts_obj, dict):
        stale_reasons.append("missing_nuts_field")
        return optional_nuts, units, stale_reasons, mint_methods, melt_methods, nut4_disabled, nut5_disabled

    for raw_key, value in nuts_obj.items():
        try:
            nut_num = int(raw_key)
        except (ValueError, TypeError):
            continue
        if not isinstance(value, dict):
            continue

        supported = value.get("supported", True)
        disabled = value.get("disabled", False)

        # Always capture method-unit pairs for NUT-04/05 so we can display them
        # even when minting/melting is disabled (with a strikethrough).
        if nut_num == 4:
            nut4_disabled = bool(disabled)
            for method in value.get("methods") or []:
                if isinstance(method, dict):
                    m_name = str(method.get("method") or "").strip()
                    unit = normalize_unit(method.get("unit"))
                    if m_name and unit:
                        mint_methods.setdefault(m_name, set()).add(unit)
        elif nut_num == 5:
            nut5_disabled = bool(disabled)
            for method in value.get("methods") or []:
                if isinstance(method, dict):
                    m_name = str(method.get("method") or "").strip()
                    unit = normalize_unit(method.get("unit"))
                    if m_name and unit:
                        melt_methods.setdefault(m_name, set()).add(unit)

        if not supported or disabled:
            continue

        if nut_num not in MANDATORY_NUTS:
            optional_nuts.append(nut_num)

        # Extract units from mint/melt methods only when enabled.
        if nut_num in (4, 5):
            for method in value.get("methods") or []:
                unit = normalize_unit(method.get("unit"))
                if unit:
                    units.append(unit)

    # If no units were discovered from NUT-04/05, that's a stale signal.
    if not units:
        stale_reasons.append("no_units_discovered")

    return (
        sorted(set(optional_nuts)),
        sorted(set(units)),
        stale_reasons,
        mint_methods,
        melt_methods,
        nut4_disabled,
        nut5_disabled,
    )


def parse_contact(contact_list: list[dict[str, Any]] | None) -> dict[str, str]:
    result: dict[str, str] = {
        "email": "",
        "x": "",
        "nostr": "",
        "other_contact": "",
    }
    if not isinstance(contact_list, list):
        return result

    extras: list[str] = []
    for entry in contact_list:
        if not isinstance(entry, dict):
            continue
        method = str(entry.get("method", "")).strip().lower()
        info = str(entry.get("info", "")).strip()
        if not info:
            continue
        if method == "email":
            result["email"] = info
        elif method in ("twitter", "x"):
            result["x"] = info
        elif method == "nostr":
            result["nostr"] = info
        else:
            extras.append(f"{method}: {info}")

    if extras:
        result["other_contact"] = "; ".join(extras)
    return result


# ---------------------------------------------------------------------------
# Discovery sources
# ---------------------------------------------------------------------------


def load_sources() -> dict[str, Any]:
    if not SOURCES_JSON.exists():
        return DEFAULT_SOURCES
    try:
        loaded = json.loads(SOURCES_JSON.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Warning: could not read {SOURCES_JSON}: {exc}", file=sys.stderr)
        return DEFAULT_SOURCES
    sources = dict(DEFAULT_SOURCES)
    if isinstance(loaded, dict):
        for key, value in loaded.items():
            if isinstance(value, dict):
                sources[key] = {**sources.get(key, {}), **value}
    return sources


def _candidate(raw: str, source: str, **extra: Any) -> dict[str, Any]:
    url = normalize_mint_url(raw)
    return {"url": url or "", "raw": str(raw or "").strip(), "source": source, **extra}


def parse_mintradar_payload(payload: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    mints = payload.get("mints") if isinstance(payload, dict) else None
    if not isinstance(mints, list):
        return out
    for mint in mints:
        if not isinstance(mint, dict):
            continue
        url = str(mint.get("url") or "")
        if not url:
            continue
        out.append(_candidate(url, "mintradar", is_test=bool(mint.get("isTest")), status_hint=mint.get("status")))
    return out


def parse_cashumints_payload(payload: Any, types: list[str] | tuple[str, ...] = ("cashu",)) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(payload, list):
        return out
    allowed = {str(t) for t in types}
    for mint in payload:
        if not isinstance(mint, dict):
            continue
        if str(mint.get("type") or "") not in allowed:
            continue
        url = str(mint.get("url") or "")
        if not url:
            continue
        out.append(_candidate(url, "cashumints"))
    return out


def parse_nip87_events(events: list[Any]) -> list[dict[str, Any]]:
    """Extract mint URLs from NIP-87 kind 38172 announcements and 38000 recommendations."""
    found: dict[str, dict[str, Any]] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        kind = event.get("kind")
        tags = event.get("tags")
        if not isinstance(tags, list):
            continue
        if kind == 38000:
            k_tag = next((t[1] for t in tags if isinstance(t, list) and len(t) > 1 and t[0] == "k"), None)
            if k_tag not in (None, "38172"):
                continue
        d_tag = next((t[1] for t in tags if isinstance(t, list) and len(t) > 1 and t[0] == "d"), None)
        for tag in tags:
            if not (isinstance(tag, list) and len(tag) >= 2 and tag[0] == "u"):
                continue
            if not str(tag[1] or "").startswith("https://"):
                continue  # skip fedimint invite codes and other non-URL values
            kind_hint = tag[2] if len(tag) > 2 else None
            if kind_hint == "fedimint":
                continue
            if kind == 38000 and kind_hint not in (None, "cashu"):
                continue
            item = _candidate(str(tag[1] or ""), "nip87")
            if not item["url"]:
                continue
            current = found.setdefault(item["url"], {**item, "nip87_announced": False, "nip87_d": None})
            if kind == 38172:
                current["nip87_announced"] = True
                if isinstance(d_tag, str) and PUBKEY_RE.match(d_tag.strip()):
                    current["nip87_d"] = d_tag.strip().lower()
    return list(found.values())


async def _query_relay(relay: str, kinds: list[int], timeout: int) -> list[Any]:
    try:
        import websockets
    except ImportError:
        return []

    events: list[Any] = []
    try:
        async with websockets.connect(
            relay,
            open_timeout=8,
            close_timeout=2,
            max_size=2**22,
        ) as ws:
            await ws.send(json.dumps(["REQ", "cmdir", {"kinds": kinds, "limit": 1000}]))
            deadline = asyncio.get_running_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                try:
                    message = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if not isinstance(message, list) or not message:
                    continue
                if message[0] == "EOSE":
                    break
                if message[0] == "EVENT" and len(message) >= 3:
                    events.append(message[2])
    except Exception as exc:  # noqa: BLE001 - a broken relay must not abort discovery
        print(f"Warning: NIP-87 relay {relay} failed: {exc}", file=sys.stderr)
    return events


async def _query_relays(relays: list[str], kinds: list[int], timeout: int) -> list[Any]:
    results = await asyncio.gather(*(_query_relay(relay, kinds, timeout) for relay in relays))
    merged: list[Any] = []
    seen: set[str] = set()
    for events in results:
        for event in events:
            event_id = event.get("id") if isinstance(event, dict) else None
            if event_id and event_id in seen:
                continue
            if event_id:
                seen.add(event_id)
            merged.append(event)
    return merged


def discover_from_nip87(config: dict[str, Any]) -> list[dict[str, Any]]:
    if not config.get("enabled", True):
        return []
    relays = [str(r) for r in config.get("relays") or []]
    kinds = [int(k) for k in (config.get("kinds") or [38172, 38000])]
    timeout = int(config.get("timeout_seconds") or 10)
    if not relays:
        return []
    try:
        import websockets  # noqa: F401 - presence check
    except ImportError:
        print("Warning: websockets is not installed; skipping NIP-87 discovery", file=sys.stderr)
        return []
    events = asyncio.run(_query_relays(relays, kinds, timeout))
    return parse_nip87_events(events)


def read_manual_mints() -> list[str]:
    """Return normalized URLs listed in data/manual_mints.txt."""
    if not MANUAL_MINTS.exists():
        return []
    urls: list[str] = []
    for line in MANUAL_MINTS.read_text(encoding="utf-8-sig").splitlines():
        line = line.split("#", 1)[0].strip()
        url = normalize_mint_url(line)
        if url and url not in urls:
            urls.append(url)
    return urls


def discover_mints(
    existing: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], dict[str, Counter]]:
    """
    Aggregate discovery results.

    Returns (meta, candidates, stats) where meta maps a case-insensitive
    dedupe key -> {url, sources, is_test, nip87_announced, nip87_d};
    candidates are URLs that were rejected before probing; stats holds
    per-source counts.
    """
    sources = load_sources()
    meta: dict[str, dict[str, Any]] = {}
    candidates: list[dict[str, Any]] = []
    stats: dict[str, Counter] = {"discovered": Counter(), "new": Counter(), "rejected": Counter()}
    priorities = {"existing": 0, "manual": 1, "mintradar": 2, "cashumints": 3, "nip87": 4}

    existing_keys = set()
    for mint in existing:
        url = normalize_mint_url(str(mint.get("url", "")))
        if url:
            existing_keys.add(dedupe_key(url))

    def add(item: dict[str, Any], allow_new: bool = True, reject_reason: str = "source_new_cap") -> None:
        source = str(item.get("source") or "unknown")
        stats["discovered"][source] += 1
        url = str(item.get("url") or "")
        if not url:
            stats["rejected"][source] += 1
            candidates.append({
                "raw": item.get("raw", ""),
                "source": source,
                "reason": "invalid_url",
                "first_seen": iso_now(),
            })
            return
        host = urlsplit(url).hostname or ""
        ok, why = host_is_admissible(host)
        if not ok:
            stats["rejected"][source] += 1
            candidates.append({"url": url, "source": source, "reason": f"host {why}", "first_seen": iso_now()})
            return
        key = dedupe_key(url)
        if not allow_new and key not in existing_keys and key not in meta:
            stats["rejected"][source] += 1
            candidates.append({"url": url, "source": source, "reason": reject_reason, "first_seen": iso_now()})
            return
        entry = meta.get(key)
        if entry is None:
            entry = {
                "url": url,
                "key": key,
                "sources": set(),
                "is_test": False,
                "nip87_announced": False,
                "nip87_d": None,
                "_priority": priorities.get(source, 9),
            }
            meta[key] = entry
        elif priorities.get(source, 9) < entry["_priority"]:
            # Prefer the canonical casing from a more authoritative source.
            entry["url"] = url
            entry["_priority"] = priorities.get(source, 9)
        entry["sources"].add(source)
        if item.get("is_test"):
            entry["is_test"] = True
        if item.get("nip87_announced"):
            entry["nip87_announced"] = True
        if item.get("nip87_d"):
            entry["nip87_d"] = item["nip87_d"]
        if key not in existing_keys:
            stats["new"][source] += 1

    for mint in existing:
        url = normalize_mint_url(str(mint.get("url", "")))
        if url:
            add({"url": url, "source": "existing"})

    for url in read_manual_mints():
        add({"url": url, "source": "manual"})

    mintradar = sources.get("mintradar") or {}
    if mintradar.get("enabled", True):
        status, payload = http_get(str(mintradar.get("endpoint") or DEFAULT_SOURCES["mintradar"]["endpoint"]))
        if status == 200:
            for item in parse_mintradar_payload(payload):
                add(item)
        else:
            print(f"Warning: mintradar source returned HTTP {status}", file=sys.stderr)

    cashumints = sources.get("cashumints") or {}
    if cashumints.get("enabled", True):
        status, payload = http_get(str(cashumints.get("endpoint") or DEFAULT_SOURCES["cashumints"]["endpoint"]))
        if status == 200:
            for item in parse_cashumints_payload(payload, cashumints.get("types") or ["cashu"]):
                add(item)
        else:
            print(f"Warning: cashumints source returned HTTP {status}", file=sys.stderr)

    nip87 = sources.get("nip87") or {}
    if nip87.get("enabled", True):
        items = discover_from_nip87(nip87)
        for item in items:
            url = str(item.get("url") or "")
            key = dedupe_key(url) if url else ""
            # NIP-87 is untrusted input: a mint announced only there (not in an
            # aggregator, the manual list, or existing data) goes to the review
            # queue instead of the published table.
            nip87_only = bool(key) and key not in existing_keys and key not in meta
            add(item, allow_new=not nip87_only, reject_reason="nip87_unverified")

    return meta, candidates, stats


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------


def offline_record(url: str, reasons: list[str] | None = None) -> dict[str, Any]:
    return {
        "url": url,
        "status": "offline",
        "last_seen": iso_now(),
        "name": hostname_from_url(url),
        "implementation": "",
        "version": "",
        "nuts": [],
        "units": [],
        "email": "",
        "x": "",
        "nostr": "",
        "other_contact": "",
        "icon_url": "",
        "description": "",
        "description_long": "",
        "mint_methods": [],
        "melt_methods": [],
        "nut4_disabled": False,
        "nut5_disabled": False,
        "pubkey": "",
        "time_offset_seconds": None,
        "stale_score": 0,
        "stale_reasons": reasons or ["offline"],
    }


def probe_mint(url: str) -> dict[str, Any]:
    """
    Probe a single mint and return a normalized record.
    If the probe fails, the record will have status='offline' and whatever
    metadata we can infer from the URL.
    """
    host = urlsplit(url).hostname or ""
    if resolve_host(host)[0] == "private":
        return offline_record(url, ["offline", "non_public_host"])

    info_url = urljoin(url.rstrip("/") + "/", "v1/info")
    status_code, info = http_get(info_url)
    online = status_code == 200 and isinstance(info, dict) and "nuts" in info

    record: dict[str, Any] = {
        "url": url,
        "status": "online" if online else "offline",
        "last_seen": iso_now(),
    }

    if not online:
        return offline_record(url)

    impl, version = parse_version(info.get("version"))
    if impl:
        impl = normalize_implementation(impl)
    else:
        impl = "Unknown"

    name = str(info.get("name") or "").strip().strip('"').strip("'") or hostname_from_url(url)
    (
        optional_nuts,
        units,
        parse_reasons,
        mint_methods,
        melt_methods,
        nut4_disabled,
        nut5_disabled,
    ) = parse_nuts(info.get("nuts"))
    contact = parse_contact(info.get("contact"))

    server_time = info.get("time")
    time_offset = None
    if isinstance(server_time, (int, float)):
        time_offset = int(abs(now_utc().timestamp() - float(server_time)))

    record.update(
        {
            "name": name,
            "implementation": impl,
            "version": version or "",
            "nuts": optional_nuts,
            "units": units,
            "email": contact["email"],
            "x": contact["x"],
            "nostr": contact["nostr"],
            "other_contact": contact["other_contact"],
            "icon_url": str(info.get("icon_url") or "").strip(),
            "description": str(info.get("description") or "").strip(),
            "description_long": str(info.get("description_long") or "").strip(),
            "mint_methods": [{"method": m, "units": sorted(us)} for m, us in sorted(mint_methods.items())],
            "melt_methods": [{"method": m, "units": sorted(us)} for m, us in sorted(melt_methods.items())],
            "nut4_disabled": nut4_disabled,
            "nut5_disabled": nut5_disabled,
            "pubkey": str(info.get("pubkey") or "").strip(),
            "time_offset_seconds": time_offset,
            "stale_reasons": parse_reasons,
        }
    )
    return record


def hostname_from_url(url: str) -> str:
    try:
        return urlparse(url).hostname or url
    except Exception:
        return url


def merge_with_existing(
    probed: dict[str, Any], existing_lookup: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """
    Preserve manually curated contact info from the existing file if the mint
    is still online and the probe didn't return its own contact data.
    """
    old = existing_lookup.get(dedupe_key(probed["url"]))
    if not old:
        return probed

    # If offline, keep old name/implementation/version but mark offline.
    if probed["status"] == "offline":
        for key in ("name", "implementation", "version", "units"):
            if old.get(key):
                probed[key] = old[key]
        # Convert legacy integer nuts field to list.
        old_nuts = old.get("nuts")
        if isinstance(old_nuts, int):
            probed["nuts"] = list(range(7, 7 + old_nuts)) if old_nuts else []
        elif isinstance(old_nuts, list):
            probed["nuts"] = old_nuts
        # Keep contact and presentation info too.
        for key in (
            "email",
            "x",
            "nostr",
            "other_contact",
            "icon_url",
            "description",
            "description_long",
            "mint_methods",
            "melt_methods",
            "nut4_disabled",
            "nut5_disabled",
            "pubkey",
        ):
            if old.get(key):
                probed[key] = old[key]
        probed["mint_methods"] = normalize_methods(probed.get("mint_methods"))
        probed["melt_methods"] = normalize_methods(probed.get("melt_methods"))
        return probed

    # Online: backfill empty contact and presentation fields from the existing record.
    for key in (
        "email",
        "x",
        "nostr",
        "other_contact",
        "icon_url",
        "description",
        "description_long",
        "mint_methods",
        "melt_methods",
        "nut4_disabled",
        "nut5_disabled",
        "pubkey",
    ):
        if not probed.get(key) and old.get(key):
            probed[key] = old[key]

    # Ensure method fields are in the canonical structured form.
    probed["mint_methods"] = normalize_methods(probed.get("mint_methods"))
    probed["melt_methods"] = normalize_methods(probed.get("melt_methods"))

    return probed


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def fetch_release_table() -> dict[str, dict[str, str]]:
    """Merge GitHub Releases data over the static fallback table."""
    table = {impl: dict(versions) for impl, versions in IMPLEMENTATION_RELEASES.items()}
    for impl, repo in GITHUB_RELEASE_REPOS.items():
        status, data = http_get(f"https://api.github.com/repos/{repo}/releases?per_page=100")
        if status != 200 or not isinstance(data, list):
            print(f"Warning: release lookup for {repo} returned HTTP {status}", file=sys.stderr)
            continue
        dynamic: dict[str, str] = {}
        for release in data:
            if not isinstance(release, dict) or release.get("draft"):
                continue
            tag = str(release.get("tag_name") or "")
            if re.search(r"-(rc|alpha|beta|pre)", tag, re.I):
                continue
            match = re.search(r"(\d+\.\d+\.\d+)", tag)
            published = str(release.get("published_at") or release.get("created_at") or "")[:10]
            if match and re.fullmatch(r"\d{4}-\d{2}-\d{2}", published):
                dynamic.setdefault(match.group(1), published)
        table.setdefault(impl, {}).update(dynamic)
    return table


def version_days_old(
    version: str | None, implementation: str | None, releases: dict[str, dict[str, str]] | None = None
) -> int | None:
    """
    Return days since a release date relative to the newest known release of
    the same implementation. Returns None if the version is unknown.
    """
    if not version or not implementation:
        return None
    impl_key = implementation.lower()
    table = (releases or IMPLEMENTATION_RELEASES).get(impl_key, {})
    release_str = table.get(version)
    if not release_str:
        return None
    try:
        release = datetime.strptime(release_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        newest = max(
            (datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc) for d in table.values()),
            default=release,
        )
    except ValueError:
        return None
    return max(0, (newest - release).days)


def compute_freshness_score(
    record: dict[str, Any],
    median_nut_count: float,
    releases: dict[str, dict[str, str]] | None = None,
) -> int:
    """
    Compute a freshness score 0-100. Higher is fresher.
    Components:
      - online: 40 pts if online
      - nut_recency: up to 25 pts based on optional NUT count vs median
      - version_age: up to 20 pts if version is known and <= 180 days old
      - contact: 10 pts if any contact method is present
      - time_sync: 5 pts if /v1/info.time is within 5 minutes of UTC
    """
    score = 0
    reasons: list[str] = list(record.get("stale_reasons", []))

    # Online
    if record.get("status") == "online":
        score += WEIGHTS["online"]
    else:
        reasons.append("offline")

    # NUT recency
    nut_count = len(record.get("nuts", []))
    if median_nut_count > 0:
        ratio = min(nut_count / median_nut_count, 1.0)
    else:
        ratio = 1.0 if nut_count > 0 else 0.0
    score += int(WEIGHTS["nut_recency"] * ratio)
    if ratio < 0.5:
        reasons.append("low_nut_support")

    # Version age
    days_old = version_days_old(record.get("version"), record.get("implementation"), releases)
    if days_old is not None:
        if days_old <= VERSION_AGE_THRESHOLD_DAYS:
            score += WEIGHTS["version_age"]
        else:
            reasons.append("version_too_old")
    else:
        # Unknown version age is a slight stale signal.
        score += WEIGHTS["version_age"] // 2
        reasons.append("unknown_version_age")

    # Contact info
    has_contact = any(
        record.get(k) for k in ("email", "x", "nostr", "other_contact")
    )
    if has_contact:
        score += WEIGHTS["contact"]
    else:
        reasons.append("missing_contact")

    # Time sync: compare the mint's reported time with ours.
    offset = record.get("time_offset_seconds")
    if offset is None:
        reasons.append("unknown_time")
    elif offset <= TIME_SYNC_TOLERANCE_SECONDS:
        score += WEIGHTS["time_sync"]
    else:
        reasons.append("clock_skew")

    record["stale_score"] = max(0, min(100, score))
    # Deduplicate and sort reasons.
    record["stale_reasons"] = sorted(set(reasons))
    return record["stale_score"]


def sort_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort by status (online first), then implementation, then version desc."""

    def semver_key(v: str) -> tuple[int, int, int, int]:
        # Strip leading non-digits and trailing pre-release.
        v = re.sub(r"^[^0-9]*", "", v or "")
        v = re.sub(r"-.*", "", v)
        parts = v.split(".") if v else []
        padded = (parts + ["0", "0", "0", "0"])[:4]
        return tuple(int(p) if p.isdigit() else 0 for p in padded)  # type: ignore[return-value]

    return sorted(
        records,
        key=lambda r: (
            0 if r.get("status") == "online" else 1,
            str(r.get("implementation", "")).lower() or "zzzz",
            -semver_key(str(r.get("version", "")))[0],
            -semver_key(str(r.get("version", "")))[1],
            -semver_key(str(r.get("version", "")))[2],
            str(r.get("name", "")).lower(),
        ),
    )


def add_display_fields(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for r in records:
        r["nuts_display"] = ",".join(str(n) for n in r.get("nuts", []))
        r["units_display"] = ",".join(r.get("units", []))
        r["mint_methods_display"] = ",".join(m["method"] for m in r.get("mint_methods", []))
        r["melt_methods_display"] = ",".join(m["method"] for m in r.get("melt_methods", []))
        r["discovered_via_display"] = ",".join(r.get("discovered_via", []))
        r["aliases_display"] = ",".join(r.get("aliases", []))
    return records


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def dedupe_records(
    records: list[dict[str, Any]], preferred_keys: set[str]
) -> list[dict[str, Any]]:
    """
    Collapse records that share a mint pubkey (same mint, different URL casing
    or alias), keeping the preferred/canonical URL and merging provenance.
    """

    def sort_key(record: dict[str, Any]) -> tuple[int, int]:
        return (0 if dedupe_key(record["url"]) in preferred_keys else 1, len(record["url"]))

    kept_by_pubkey: dict[str, dict[str, Any]] = {}
    output: list[dict[str, Any]] = []
    for record in sorted(records, key=sort_key):
        pubkey = str(record.get("pubkey") or "").lower()
        if record.get("status") == "online" and pubkey:
            existing = kept_by_pubkey.get(pubkey)
            if existing is not None:
                aliases = set(existing.get("aliases") or [])
                aliases.update(record.get("aliases") or [])
                aliases.add(record["url"])
                aliases.discard(existing["url"])
                existing["aliases"] = sorted(aliases)
                merged_sources = set(existing.get("discovered_via") or []) | set(record.get("discovered_via") or [])
                existing["discovered_via"] = sorted(merged_sources - {"existing"}) or ["existing"]
                existing["first_seen"] = min(
                    existing.get("first_seen") or record["first_seen"], record["first_seen"]
                )
                existing["is_test"] = bool(existing.get("is_test") or record.get("is_test"))
                existing["nip87_announced"] = bool(
                    existing.get("nip87_announced") or record.get("nip87_announced")
                )
                if existing.get("nip87_pubkey_match") is None:
                    existing["nip87_pubkey_match"] = record.get("nip87_pubkey_match")
                continue
            kept_by_pubkey[pubkey] = record
        output.append(record)
    return output


def load_candidates() -> list[dict[str, Any]]:
    if not CANDIDATES_JSON.exists():
        return []
    try:
        data = json.loads(CANDIDATES_JSON.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def stabilize_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep first_seen stable so an unchanged candidate list does not churn commits."""
    previous = {
        (str(c.get("url") or c.get("raw")), str(c.get("source"))): c
        for c in load_candidates()
        if isinstance(c, dict)
    }
    for candidate in candidates:
        key = (str(candidate.get("url") or candidate.get("raw")), str(candidate.get("source")))
        old = previous.get(key)
        if old and old.get("first_seen"):
            candidate["first_seen"] = old["first_seen"]
    return candidates


def load_existing() -> list[dict[str, Any]]:
    if not MINTS_JSON.exists():
        return []
    try:
        with MINTS_JSON.open("r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as exc:
        print(f"Warning: could not read existing {MINTS_JSON}: {exc}", file=sys.stderr)
        return []


def build_report(
    records: list[dict[str, Any]],
    stats: dict[str, Counter],
    candidates: list[dict[str, Any]],
    new_urls: list[str],
) -> str:
    online = [r for r in records if r.get("status") == "online"]
    new_set = set(new_urls)
    new_by_source: Counter = Counter()
    for record in records:
        if record["url"] in new_set:
            for source in record.get("discovered_via") or []:
                new_by_source[source] += 1
    lines = [
        "# Mint directory update",
        "",
        f"- Probed **{len(records)}** mints: {len(online)} online, {len(records) - len(online)} offline.",
        f"- New this run: **{len(new_urls)}**.",
        "",
        "| Source | Discovered | New | Rejected |",
        "|---|---:|---:|---:|",
    ]
    for source in sorted(s for s in stats["discovered"] if s != "existing"):
        lines.append(
            f"| {source} | {stats['discovered'][source]} | {new_by_source[source]} | {stats['rejected'][source]} |"
        )
    if new_urls:
        lines += ["", "## New mints", ""] + [f"- {url}" for url in sorted(new_urls)]
    if candidates:
        lines += ["", "## Candidates for review", ""]
        lines += [f"- `{c.get('url') or c.get('raw')}` ({c.get('source')}): {c.get('reason')}" for c in candidates[:50]]
        if len(candidates) > 50:
            lines.append(f"- … and {len(candidates) - 50} more in data/candidates.json")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Discover, probe and score Cashu mints.")
    parser.add_argument("--dry-run", action="store_true", help="do not write mints.json or candidates.json")
    parser.add_argument("--report", default="", help="write a markdown report to this path ('-' for stdout)")
    args = parser.parse_args(argv)

    existing = load_existing()
    existing_lookup: dict[str, dict[str, Any]] = {}
    existing_norm: set[str] = set()
    for mint in existing:
        normalized = normalize_mint_url(str(mint.get("url", "")))
        if not normalized:
            continue
        existing_norm.add(normalized)
        existing_lookup[dedupe_key(normalized)] = mint

    meta, candidates, stats = discover_mints(existing)
    candidates = stabilize_candidates(candidates)
    meta_by_key = {item["key"]: item for item in meta.values()}
    urls = sorted({item["url"] for item in meta.values()})
    print(f"Discovered {len(urls)} mint URLs to probe")
    for source in sorted(stats["discovered"]):
        print(f"  {source}: {stats['discovered'][source]} seen, {stats['new'][source]} new")

    releases = fetch_release_table()
    records: list[dict[str, Any]] = []
    for url in urls:
        probed = probe_mint(url)
        merged = merge_with_existing(probed, existing_lookup)
        records.append(merged)

    # Provenance, test flag, NIP-87 verification.
    now = iso_now()
    for record in records:
        url = record["url"]
        source_meta = meta_by_key.get(dedupe_key(url), {})
        old = existing_lookup.get(dedupe_key(url), {})
        sources = set(source_meta.get("sources") or [])
        old_sources = old.get("discovered_via") or []
        if isinstance(old_sources, list):
            sources.update(str(s) for s in old_sources)
        # "existing" means retention, not discovery; keep it only for records
        # that no live source has ever surfaced.
        record["discovered_via"] = sorted(s for s in sources if s and s != "existing") or ["existing"]
        record["first_seen"] = old.get("first_seen") or now
        record["is_test"] = bool(
            source_meta.get("is_test")
            or old.get("is_test")
            or is_test_mint(url, old.get("is_test"))
        )
        announced = bool(source_meta.get("nip87_announced") or old.get("nip87_announced"))
        record["nip87_announced"] = announced
        d_tag = source_meta.get("nip87_d") or old.get("nip87_d")
        pubkey = str(record.get("pubkey") or "").strip().lower()
        if d_tag and pubkey and PUBKEY_RE.match(pubkey):
            record["nip87_pubkey_match"] = d_tag.lower() == pubkey
        elif old.get("nip87_pubkey_match") is not None:
            record["nip87_pubkey_match"] = old.get("nip87_pubkey_match")
        else:
            record["nip87_pubkey_match"] = None
        if d_tag:
            record["nip87_d"] = d_tag
        elif old.get("nip87_d"):
            record["nip87_d"] = old["nip87_d"]

    preferred_keys = {dedupe_key(url) for url in existing_norm}
    preferred_keys.update(dedupe_key(url) for url in read_manual_mints())
    records = dedupe_records(records, preferred_keys)

    # Compute median optional-NUT count among online mints for scoring.
    online_nut_counts = [len(r["nuts"]) for r in records if r.get("status") == "online"]
    median_nut_count = float(sorted(online_nut_counts or [0])[len(online_nut_counts or [0]) // 2])

    for r in records:
        compute_freshness_score(r, median_nut_count, releases)

    records = sort_records(records)
    records = add_display_fields(records)

    new_urls = [r["url"] for r in records if r["url"] not in existing_norm]
    report = build_report(records, stats, candidates, new_urls)

    if not args.dry_run:
        MINTS_JSON.parent.mkdir(parents=True, exist_ok=True)
        with MINTS_JSON.open("w", encoding="utf-8", newline="\n") as f:
            json.dump(records, f, indent=2, ensure_ascii=False)
            f.write("\n")
        CANDIDATES_JSON.parent.mkdir(parents=True, exist_ok=True)
        with CANDIDATES_JSON.open("w", encoding="utf-8", newline="\n") as f:
            json.dump(candidates, f, indent=2, ensure_ascii=False)
            f.write("\n")

    if args.report:
        if args.report == "-":
            print(report)
        else:
            Path(args.report).write_text(report, encoding="utf-8", newline="\n")

    online = sum(1 for r in records if r.get("status") == "online")
    print(f"Wrote {len(records)} mints ({online} online) to {MINTS_JSON}" + (" [dry run]" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
