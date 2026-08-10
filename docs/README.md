# Cashu Mint Directory

This branch hosts the interactive Cashu mint directory.

Visit the live table:

**[https://ye0man.github.io/cashu-mint-directory/](https://ye0man.github.io/cashu-mint-directory/)**

The table is built with [Tabulator](https://tabulator.info/) and fed by [`mints.json`](./mints.json). It supports sorting, filtering, column resizing, reordering, CSV/JSON export, and pagination.

New filters include:

- **Supported NUTs** multi-select: show mints that support every selected NUT.
- **Units** multi-select: filter by supported currency units.
- **Stale/offline toggle**: hide less-maintained mints by default.

Data is updated automatically via GitHub Actions every 6 hours.

