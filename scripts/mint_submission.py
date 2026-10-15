#!/usr/bin/env python3
"""
Validate and record community mint submissions (GitHub issue form -> PR).

Subcommands
-----------
validate   Parse the issue body (env ISSUE_BODY), normalize the mint URL,
           reject duplicates / unsafe hosts, probe /v1/info, and write a
           markdown report. Sets `valid`, `url`, and `host` step outputs.
add        Append a normalized URL to data/manual_mints.txt (idempotent).

Only the standard library is used. Untrusted input (the issue body) is read
from the environment, never interpolated into shell commands.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import socket
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import update_mints  # noqa: E402  (shared probe + normalization helpers)

PROJECT_ROOT = update_mints.PROJECT_ROOT
MINTS_JSON = update_mints.MINTS_JSON
MANUAL_MINTS = update_mints.MANUAL_MINTS

BOT_MARKER = "<!-- mint-submission-bot -->"
PATH_RE = re.compile(r"^[A-Za-z0-9._~/-]*$")
HOST_RE = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}$")


# ---------------------------------------------------------------------------
# URL handling
# ---------------------------------------------------------------------------


def normalize_mint_url(raw: str) -> str | None:
    """
    Normalize a mint URL. Mirrors normalizeMintUrl() in docs/assets/app.js:
    https only, lowercase host, optional port, no credentials/query/fragment,
    trailing slash and a trailing /v1/info stripped.
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


def host_is_public(host: str) -> tuple[bool, str]:
    """Resolve a hostname and refuse private, loopback, or otherwise non-global targets."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        return False, f"DNS lookup failed ({exc.strerror or exc})"
    addrs = {info[4][0] for info in infos}
    for addr in addrs:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
        if not ip.is_global:
            return False, f"resolves to a non-public address ({ip})"
    return True, ""


def extract_url_from_issue(body: str) -> str:
    """Pull the URL from the issue form's 'Mint URL' section, falling back to the first URL."""
    body = body or ""
    match = re.search(r"^#{2,4}\s*Mint URL\s*$\s*(.+?)\s*(?=^#{2,4}\s|\Z)", body, re.M | re.S | re.I)
    if match:
        section = match.group(1).strip()
        first = section.splitlines()[0].strip() if section else ""
        if first and first != "_No response_":
            return first
    fallback = re.search(r"https?://[^\s<>()\[\]`'\"]+", body)
    return fallback.group(0) if fallback else ""


def known_urls() -> set[str]:
    urls: set[str] = set()
    if MINTS_JSON.exists():
        try:
            for mint in json.loads(MINTS_JSON.read_text(encoding="utf-8")):
                n = normalize_mint_url(str(mint.get("url", "")))
                if n:
                    urls.add(n)
        except (OSError, json.JSONDecodeError):
            pass
    for url in update_mints.read_manual_mints():
        n = normalize_mint_url(url)
        if n:
            urls.add(n)
    return urls


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def md_safe(value: object, limit: int = 120) -> str:
    """Make mint-supplied text safe to embed in a markdown table cell."""
    text = re.sub(r"[\r\n\t|`<>*_\[\]]", " ", str(value or "")).strip()
    text = text.replace("@", "@\u200b")  # avoid pinging users
    text = re.sub(r"\s+", " ", text)
    return (text[: limit - 1] + "…") if len(text) > limit else text or "—"


def methods_summary(methods: list[dict]) -> str:
    return ", ".join(
        f"{m.get('method')} ({'/'.join(m.get('units') or []) or '?'})" for m in methods or []
    ) or "—"


def build_report(url: str | None, raw: str, valid: bool, problems: list[str], record: dict | None) -> str:
    lines = [BOT_MARKER]
    if valid:
        lines.append("### Mint check: passed")
    else:
        lines.append("### Mint check: needs attention")
    lines.append("")
    lines.append("| | |")
    lines.append("|---|---|")
    lines.append(f"| Submitted | `{md_safe(raw, 200)}` |")
    lines.append(f"| Normalized | `{url}` |" if url else "| Normalized | — |")
    if record:
        lines.append(f"| Status | {record.get('status')} |")
    if record and record.get("status") == "online":
        lines.append(f"| Name | {md_safe(record.get('name'))} |")
        impl = record.get("implementation") or "?"
        lines.append(f"| Implementation | {md_safe(impl)} {md_safe(record.get('version') or '')} |")
        lines.append(f"| Units | {md_safe(', '.join(record.get('units') or []))} |")
        lines.append(f"| Mint methods (NUT-04) | {md_safe(methods_summary(record.get('mint_methods')), 200)}{' (disabled)' if record.get('nut4_disabled') else ''} |")
        lines.append(f"| Melt methods (NUT-05) | {md_safe(methods_summary(record.get('melt_methods')), 200)}{' (disabled)' if record.get('nut5_disabled') else ''} |")
        nuts = record.get("nuts") or []
        lines.append(f"| Optional NUTs | {', '.join(str(n) for n in nuts) or '—'} ({len(nuts)}) |")
        has_contact = any(record.get(k) for k in ("email", "x", "nostr", "other_contact"))
        lines.append(f"| Contact info | {'yes' if has_contact else 'none published'} |")
    lines.append("")
    if problems:
        lines.append("**Problems:**")
        lines.extend(f"- {p}" for p in problems)
        lines.append("")
        lines.append("Edit the issue to fix the URL and this check will re-run automatically.")
    else:
        lines.append("A maintainer can comment **`approved`** to open a pull request adding this mint to `data/manual_mints.txt`.")
        lines.append("Once merged, the next directory update probes it and adds it to the table.")
    lines.append("")
    lines.append("<sub>Automated check by `.github/workflows/mint-submission.yml`.</sub>")
    return "\n".join(lines) + "\n"


def set_outputs(**values: str) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        for k, v in values.items():
            print(f"{k}={v}")
        return
    with open(out, "a", encoding="utf-8") as f:
        for k, v in values.items():
            f.write(f"{k}={v}\n")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_validate(args: argparse.Namespace) -> int:
    raw = args.url if args.url else extract_url_from_issue(os.environ.get("ISSUE_BODY", ""))
    problems: list[str] = []
    record: dict | None = None

    url = normalize_mint_url(raw)
    if not raw:
        problems.append("No mint URL found in the issue. Use the **Mint URL** field.")
    elif not url:
        problems.append("The URL is not valid. It must be an `https://` URL with a public hostname and no query string or credentials.")

    if url:
        if url in known_urls():
            problems.append("This mint is already in the directory.")
        else:
            host = urlsplit(url).hostname or ""
            ok, why = host_is_public(host)
            if not ok:
                problems.append(f"Host `{host}` {why}.")
            else:
                record = update_mints.probe_mint(url)
                if record.get("status") != "online":
                    problems.append("`GET /v1/info` did not return a valid NUT-06 response (HTTP 200 with a `nuts` object).")
                elif record.get("nut4_disabled") and record.get("nut5_disabled"):
                    problems.append("Both minting (NUT-04) and melting (NUT-05) are disabled.")

    valid = not problems and record is not None
    report = build_report(url, raw, valid, problems, record)
    Path(args.report).write_text(report, encoding="utf-8")
    set_outputs(valid="true" if valid else "false", url=url or "", host=(urlsplit(url).hostname if url else "") or "")
    print(report)
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    url = normalize_mint_url(args.url)
    if not url:
        print(f"Refusing to add invalid URL: {args.url!r}", file=sys.stderr)
        return 1
    MANUAL_MINTS.parent.mkdir(parents=True, exist_ok=True)
    lines = MANUAL_MINTS.read_text(encoding="utf-8").splitlines() if MANUAL_MINTS.exists() else []
    header = [line for line in lines if line.strip().startswith("#") or not line.strip()]
    entries = {normalize_mint_url(line) or line.strip() for line in lines if line.strip() and not line.strip().startswith("#")}
    if url in entries:
        print(f"{url} already present in {MANUAL_MINTS.relative_to(PROJECT_ROOT)}")
        return 0
    entries.add(url)
    while header and not header[-1].strip():
        header.pop()
    content = "\n".join(header + [""] + sorted(entries)) + "\n"
    MANUAL_MINTS.write_text(content, encoding="utf-8")
    print(f"Added {url} to {MANUAL_MINTS.relative_to(PROJECT_ROOT)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    v = sub.add_parser("validate", help="validate a submission (reads ISSUE_BODY unless --url is given)")
    v.add_argument("--url", help="validate this URL instead of parsing ISSUE_BODY")
    v.add_argument("--report", default="mint-report.md", help="where to write the markdown report")
    v.set_defaults(func=cmd_validate)

    a = sub.add_parser("add", help="append a URL to data/manual_mints.txt")
    a.add_argument("--url", required=True)
    a.set_defaults(func=cmd_add)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
