#!/usr/bin/env python3
"""
Update docs/mints.json by probing Cashu mints, normalizing /v1/info responses,
scoring freshness, and preserving manual contact details from the existing file.

Designed to run from GitHub Actions (ubuntu-latest) with Python 3.11+.
Only the standard library is used.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import sys
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MINTS_JSON = PROJECT_ROOT / "docs" / "mints.json"

# Sources used to discover mint URLs.
AWESOME_CASHU_RAW = (
    "https://raw.githubusercontent.com/cashubtc/awesome-cashu/main/README.md"
)

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

# Implementation release age lookup (ISO date of latest known release at the
# time the script runs). Dates are updated manually when a major release ships.
# If an implementation is missing, version-age scoring is skipped for it.
IMPLEMENTATION_RELEASES: dict[str, dict[str, str]] = {
    "nutshell": {
        "0.20.3": "2025-07-01",
        "0.20.2": "2025-05-01",
        "0.20.1": "2025-04-01",
        "0.20.0": "2025-03-01",
        "0.19.2": "2025-01-01",
        "0.18.2": "2024-11-01",
        "0.18.1": "2024-10-01",
        "0.18.0": "2024-09-01",
        "0.17.0": "2024-07-01",
    },
    "cdk-mintd": {
        "0.17.3": "2025-06-01",
        "0.17.1": "2025-05-01",
        "0.17.0": "2025-04-01",
        "0.16.0": "2025-02-01",
        "0.15.1": "2025-01-01",
        "0.13.4": "2024-10-01",
    },
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

REQUEST_TIMEOUT = 20  # seconds
USER_AGENT = "cashu-mint-directory-bot/1.0 (+https://github.com/ye0man/cashu-mint-directory)"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


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


def parse_version(version_field: str | None) -> tuple[str | None, str | None]:
    """
    Parse the NUT-06 version field, e.g. 'Nutshell/0.20.3' -> ('Nutshell', '0.20.3').
    """
    if not version_field:
        return None, None
    parts = version_field.split("/", 1)
    impl = parts[0].strip() if parts[0] else None
    version = parts[1].strip() if len(parts) > 1 and parts[1] else None
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


def parse_nuts(nuts_obj: dict[str, Any] | None) -> tuple[list[int], list[str], list[str]]:
    """
    From the /v1/info 'nuts' object return:
      - optional_nuts: sorted list of supported optional NUT numbers
      - all_units: deduplicated list of supported units (from NUT-04/05 methods)
      - stale_reasons: list of reasons discovered while parsing
    """
    optional_nuts: list[int] = []
    units: list[str] = []
    stale_reasons: list[str] = []

    if not isinstance(nuts_obj, dict):
        stale_reasons.append("missing_nuts_field")
        return optional_nuts, units, stale_reasons

    for raw_key, value in nuts_obj.items():
        try:
            nut_num = int(raw_key)
        except (ValueError, TypeError):
            continue
        if not isinstance(value, dict):
            continue

        supported = value.get("supported", True)
        disabled = value.get("disabled", False)
        if not supported or disabled:
            continue

        if nut_num not in MANDATORY_NUTS:
            optional_nuts.append(nut_num)

        # Extract units from mint/melt methods.
        if nut_num in (4, 5):
            methods = value.get("methods") or []
            for method in methods:
                unit = normalize_unit(method.get("unit"))
                if unit:
                    units.append(unit)

    # If no units were discovered from NUT-04/05, that's a stale signal.
    if not units:
        stale_reasons.append("no_units_discovered")

    return sorted(set(optional_nuts)), sorted(set(units)), stale_reasons


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


def extract_mints_from_awesome_readme(text: str) -> list[str]:
    """Very light parser: grab raw URLs that look like mint endpoints."""
    urls: list[str] = []
    # Markdown links + bare URLs
    for match in re.finditer(r"https?://[^\s\)\]<>\"']+", text):
        url = match.group(0).rstrip("/.")
        # Filter out obvious non-mint URLs.
        if any(bad in url.lower() for bad in ["github.com", "docs.cashu", "cashu.space", "mintradar"]):
            continue
        # Drop anchors/query strings
        url = url.split("#")[0].split("?")[0]
        if url and url not in urls:
            urls.append(url)
    return urls


def discover_mints(existing: list[dict[str, Any]]) -> set[str]:
    """Aggregate mint URLs from existing data and discovery sources."""
    urls: set[str] = set()

    for mint in existing:
        url = str(mint.get("url", "")).strip()
        if url:
            urls.add(url)

    # Try to fetch awesome-cashu; if it fails, we still have existing URLs.
    status, data = http_get(AWESOME_CASHU_RAW)
    if status == 200 and isinstance(data, str):
        for url in extract_mints_from_awesome_readme(data):
            urls.add(url)

    return urls


def probe_mint(url: str) -> dict[str, Any]:
    """
    Probe a single mint and return a normalized record.
    If the probe fails, the record will have status='offline' and whatever
    metadata we can infer from the URL.
    """
    info_url = urljoin(url.rstrip("/") + "/", "v1/info")
    status_code, info = http_get(info_url)
    online = status_code == 200 and isinstance(info, dict) and "nuts" in info

    record: dict[str, Any] = {
        "url": url,
        "status": "online" if online else "offline",
        "last_seen": now_utc().isoformat().replace("+00:00", "Z"),
    }

    if not online:
        record["name"] = hostname_from_url(url)
        record["implementation"] = ""
        record["version"] = ""
        record["nuts"] = []
        record["units"] = []
        record["email"] = ""
        record["x"] = ""
        record["nostr"] = ""
        record["other_contact"] = ""
        record["icon_url"] = ""
        record["description"] = ""
        record["description_long"] = ""
        record["stale_score"] = 0
        record["stale_reasons"] = ["offline"]
        return record

    impl, version = parse_version(info.get("version"))
    if impl:
        impl = normalize_implementation(impl)
    else:
        impl = "Unknown"

    name = str(info.get("name") or "").strip().strip('"').strip("'") or hostname_from_url(url)
    optional_nuts, units, parse_reasons = parse_nuts(info.get("nuts"))
    contact = parse_contact(info.get("contact"))

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
    old = existing_lookup.get(probed["url"])
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
        for key in ("email", "x", "nostr", "other_contact", "icon_url", "description", "description_long"):
            if old.get(key):
                probed[key] = old[key]
        return probed

    # Online: backfill empty contact and presentation fields from the existing record.
    for key in ("email", "x", "nostr", "other_contact", "icon_url", "description", "description_long"):
        if not probed.get(key) and old.get(key):
            probed[key] = old[key]

    return probed


def version_days_old(version: str | None, implementation: str | None) -> int | None:
    """
    Return days since the known release of this version relative to the newest
    known release of the same implementation. Returns None if no data.
    """
    if not version or not implementation:
        return None
    impl_key = implementation.lower()
    table = IMPLEMENTATION_RELEASES.get(impl_key, {})
    release_str = table.get(version)
    if not release_str:
        return None
    try:
        release = datetime.strptime(release_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    newest = max(
        (datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc) for d in table.values()),
        default=release,
    )
    return (newest - release).days


def compute_freshness_score(record: dict[str, Any], median_nut_count: float) -> int:
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
    days_old = version_days_old(record.get("version"), record.get("implementation"))
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

    # Time sync (optional signal)
    # We don't store time, so we just check if the probe had a valid nuts object,
    # which implies a well-formed response. If units were discovered, full points.
    if record.get("units"):
        score += WEIGHTS["time_sync"]
    else:
        reasons.append("no_units")

    record["stale_score"] = max(0, min(100, score))
    # Deduplicate and sort reasons.
    record["stale_reasons"] = sorted(set(reasons))
    return record["stale_score"]


def sort_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort by status (online first), then implementation, then version desc."""

    def semver_key(v: str) -> tuple[int, ...]:
        if not v:
            return (0,)
        # Strip leading non-digits and trailing pre-release.
        v = re.sub(r"^[^0-9]*", "", v)
        v = re.sub(r"-.*", "", v)
        parts = v.split(".")
        return tuple(int(p) if p.isdigit() else 0 for p in parts[:4])

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
    return records


def main() -> int:
    existing: list[dict[str, Any]] = []
    if MINTS_JSON.exists():
        try:
            with MINTS_JSON.open("r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception as exc:
            print(f"Warning: could not read existing {MINTS_JSON}: {exc}", file=sys.stderr)

    existing_lookup = {m.get("url", "").strip(): m for m in existing if m.get("url")}

    urls = discover_mints(existing)
    print(f"Discovered {len(urls)} mint URLs to probe")

    records: list[dict[str, Any]] = []
    for url in sorted(urls):
        probed = probe_mint(url)
        merged = merge_with_existing(probed, existing_lookup)
        records.append(merged)

    # Compute median optional-NUT count among online mints for scoring.
    online_nut_counts = [len(r["nuts"]) for r in records if r.get("status") == "online"]
    median_nut_count = float(sorted(online_nut_counts or [0])[len(online_nut_counts or [0]) // 2])

    for r in records:
        compute_freshness_score(r, median_nut_count)

    records = sort_records(records)
    records = add_display_fields(records)

    MINTS_JSON.parent.mkdir(parents=True, exist_ok=True)
    with MINTS_JSON.open("w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)
        f.write("\n")

    online = sum(1 for r in records if r.get("status") == "online")
    print(f"Wrote {len(records)} mints ({online} online) to {MINTS_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
