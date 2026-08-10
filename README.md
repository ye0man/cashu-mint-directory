# Cashu Mint Directory

An interactive directory of Cashu mints.

**[Open the interactive table](https://ye0man.github.io/cashu-mint-directory/)**

It is built with [Tabulator](https://tabulator.info/), is fed by [`docs/mints.json`](./docs/mints.json), and supports sorting, filtering, column resizing/reordering, and CSV/JSON export.

The data is compiled from the [Awesome Cashu](https://github.com/cashubtc/awesome-cashu) repo and direct queries to each mint’s `/v1/info` endpoint.

## Automated updates

A GitHub Actions workflow ([`.github/workflows/update-mints.yml`](./.github/workflows/update-mints.yml)) runs every 6 hours and probes every discovered mint. If any data changes, it commits the updated `docs/mints.json` directly to `main`. You can also trigger it manually from the **Actions** tab.

The update script lives at [`scripts/update_mints.py`](./scripts/update_mints.py).

## Data fields

Each mint record includes:

- `name`, `url`, `implementation`, `version`
- `nuts`: array of supported **optional** NUTs (e.g. `[7, 8, 9, ...]`)
- `units`: supported currency units (e.g. `["sat"]`), with aliases normalized
- `stale_score`: freshness score from 0–100 (higher is better)
- `stale_reasons`: signals that lowered the score
- `status`: `online` or `offline`
- `last_seen`: ISO timestamp of the last successful probe
- contact fields: `email`, `x`, `nostr`, `other_contact`

## Freshness scoring

The freshness score is computed from:

| Signal | Weight | Note |
|---|---|---|
| Online | 40 | Must return HTTP 200 with a valid `nuts` object |
| NUT support | 25 | Relative to the median optional-NUT count |
| Version age | 20 | Known implementations older than 180 days lose points |
| Contact info | 10 | Bonus for email, X, nostr, or other contact |
| Units discoverable | 5 | Bonus when mint/melt methods expose units |

By default the table hides stale and offline mints. Toggle **“Include stale / offline mints”** to see the full list.
