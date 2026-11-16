# Cashu Mint Directory

An interactive directory of Cashu mints.

**[Open the interactive table](https://ye0man.github.io/cashu-mint-directory/)**

![Cashu Mint Directory](./docs/assets/screenshot.png)

The site is a single static page (no build step) in [`docs/`](./docs): [`index.html`](./docs/index.html), [`assets/styles.css`](./docs/assets/styles.css), and [`assets/app.js`](./docs/assets/app.js). It uses [Tabulator](https://tabulator.info/) (pinned, with SRI), is fed by [`docs/mints.json`](./docs/mints.json), and supports sorting, filtering, column resizing/reordering, and CSV/JSON export of the filtered rows. When embedded in an `<iframe>` it switches to a compact layout with a transparent background.

Mint URLs are discovered from live sources ([MintRadar](https://mintradar.org), [Cashumints.space](https://cashumints.space), and NIP-87 events on Nostr), community submissions, and previously seen URLs. Every mint is then probed directly against its own `/v1/info` endpoint; source status is never trusted without that probe. See [Discovery sources](#discovery-sources).

## Features

- **Summary cards**: total mints, online share, implementations, units, BOLT12/onchain support, median freshness, and time of the last probe.
- **Filters**: free-text search, supported NUTs (all of), units, implementation, version, NUT-04 mint methods, NUT-05 melt methods, discovery source, minimum freshness, "latest version per implementation only", plus offline and test-mint toggles. **Clear filters** resets everything.
- **Readable status**: green/red online/offline indicators, colour-coded payment-method chips (BOLT11, BOLT12, Onchain, and other/fiat methods), and struck-through methods when minting or melting is disabled.
- **Provenance**: a **Source** column shows where each mint was discovered (MintRadar, Cashumints, NIP-87, submission, or seed data), and summary cards exclude test mints.
- **Responsive**: on narrow screens, lower-priority columns collapse into an expandable row.
- **Suggest a mint**: see below.
- **Resources**: links to the NUTs spec, Mint Auditor, Cashu docs, and more in the footer.

## Suggesting a mint

1. Click **Suggest a mint** on the site (or open a [*Suggest a mint* issue](https://github.com/ye0man/cashu-mint-directory/issues/new?template=suggest-mint.yml)). The dialog can test the mint's `/v1/info` from your browser first.
2. [`.github/workflows/mint-submission.yml`](./.github/workflows/mint-submission.yml) normalizes the URL, rejects duplicates and non-public hosts, probes `/v1/info`, and posts a report comment (updated in place if the issue is edited). The issue is labelled `validated` or `needs-attention`.
3. A maintainer comments **`approved`**. The workflow re-validates, adds the URL to [`data/manual_mints.txt`](./data/manual_mints.txt) on a branch, and opens a pull request that closes the issue.
4. Merging the PR triggers the update workflow, which probes the new mint and adds it to `docs/mints.json`.

Validation logic lives in [`scripts/mint_submission.py`](./scripts/mint_submission.py) and can be run locally:

```sh
python scripts/mint_submission.py validate --url https://mint.example.com --report report.md
```

> The approval step needs **Settings → Actions → General → Allow GitHub Actions to create and approve pull requests** to be enabled.

## Discovery sources

Sources are declared in [`data/sources.json`](./data/sources.json) and can be enabled or disabled without code changes. URLs that cannot be admitted (private hosts, junk entries, or NIP-87 overflow) are written to [`data/candidates.json`](./data/candidates.json) with a reason instead of being silently dropped.

| Source | Type | What it provides |
|---|---|---|
| `manual` | [`data/manual_mints.txt`](./data/manual_mints.txt) | Approved community submissions and maintainer additions |
| `mintradar` | [`mintradar.org/api/v1/wallets/mints`](https://mintradar.org/api/v1/wallets/mints) | Live + offline mints, reliability scores, software versions |
| `cashumints` | [`cashumints.space/api/mints`](https://cashumints.space/api/mints) | NIP-87-derived index with community reviews, including historical offline mints |
| `nip87` | Nostr relays | `kind:38172` mint announcements and `kind:38000` recommendations |

The existing `docs/mints.json` is always retained as a source, so historical mints do not disappear when a third-party index goes down. Auto-discovered URLs are capped per run for NIP-87 (`max_new_per_run`), and every URL is re-probed against `/v1/info` before it is published.

## Automated updates

A GitHub Actions workflow ([`.github/workflows/update-mints.yml`](./.github/workflows/update-mints.yml)) runs every 6 hours, whenever `data/manual_mints.txt` or `data/sources.json` changes on `main`, and probes every discovered mint. If any data changes, it commits the updated `docs/mints.json` and `data/candidates.json` directly to `main`. You can also trigger it manually from the **Actions** tab.

The update script lives at [`scripts/update_mints.py`](./scripts/update_mints.py). It needs Python 3.11+ and the `websockets` package for NIP-87 discovery:

```sh
python -m pip install -r requirements.txt
python scripts/update_mints.py --dry-run --report -
```

## Local development

```sh
cd docs && python -m http.server 8000
# open http://localhost:8000
```

Unit tests (no network):

```sh
python -m unittest discover -s tests
```

## Data fields

Each mint record includes:

- `name`, `url`, `implementation`, `version`
- `nuts`: array of supported **optional** NUTs (e.g. `[7, 8, 9, ...]`)
- `units`: supported currency units (e.g. `["sat"]`), with aliases normalized
- `stale_score`: freshness score from 0–100 (higher is better)
- `stale_reasons`: signals that lowered the score
- `status`: `online` or `offline`
- `last_seen`: ISO timestamp of the last probe
- `mint_methods`, `melt_methods`: arrays of structured method objects, e.g. `[{"method": "bolt11", "units": ["sat", "usd"]}]`. The table renders each method as a chip and shows its supported units on hover.
- `nut4_disabled`, `nut5_disabled`: whether minting/melting is disabled
- `pubkey`: the mint's NUT-06 pubkey, used to verify NIP-87 announcements
- `time_offset_seconds`: absolute difference between the mint's reported `time` and the probe time
- `discovered_via`: discovery sources that surfaced this mint (`mintradar`, `cashumints`, `nip87`, `manual`, `existing`)
- `first_seen`: ISO timestamp of the first run that included this mint
- `is_test`: test mint (hidden by default, excluded from summary cards)
- `nip87_announced`, `nip87_d`, `nip87_pubkey_match`: whether a NIP-87 announcement was seen, its `d` tag, and whether that tag matched the mint's pubkey
- `aliases`: other URLs that resolve to the same mint (matched by `/v1/info` pubkey)
- contact fields: `email`, `x`, `nostr`, `other_contact` (rendered in the table as a single **Contact** column with hyperlinks)

## Freshness scoring

The freshness score is computed from:

| Signal | Weight | Note |
|---|---|---|
| Online | 40 | Must return HTTP 200 with a valid `nuts` object |
| NUT support | 25 | Relative to the median optional-NUT count |
| Version age | 20 | Compares the release date of the mint's version with the newest release, using the GitHub Releases API (static fallback when unavailable) |
| Contact info | 10 | Bonus for email, X, nostr, or other contact |
| Time sync | 5 | `/v1/info.time` is within 5 minutes of UTC |

By default the table hides offline and test mints. Toggle **"Include offline mints"** / **"Include test mints"** to see them, or use the **Min. freshness** slider to hide stale ones.
