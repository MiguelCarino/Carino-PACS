# Changelog

All notable changes to Carino PACS. Versions follow [Semantic Versioning](https://semver.org/).
Licensed under **AGPL-3.0-or-later** (see [LICENSE](LICENSE)).

## [1.1.0] — unreleased

Everything added on top of the upstream **1.0.0** store-and-forward baseline.
These features are backward compatible; the core receive/auto-send behaviour is
unchanged.

### Added
- **Emergency RIS** — HL7 `ORM^O01` order intake over MLLP (default port
  `2575`), plus hand-keyed orders in the dashboard. Arriving studies are matched
  to open orders by Accession Number (Patient ID fallback) and archived for
  audit; image delivery is never gated on a match. (`pacs/ris.py`)
- **Modality Worklist (MWL)** — serves open orders to modalities via C-FIND
  (default AE/port `11114`), burning each order's Study Instance UID into the
  exam so the returned study reconciles exactly. Per-destination *No RIS* mode
  runs the worklist permanently. (`pacs/mwl.py`)
- **Emergency failover** — monitors a primary PACS by periodic C-ECHO and
  forward-failure watch; on sustained outage, prompts (or auto-activates) the
  local worklist, holds studies received during the outage, and auto-forwards
  them once the primary returns. (`pacs/emergency.py`)
- **Virtual print receiver** — captures print-only modalities as PDF.
  (`pacs/print_scp.py`)
- **Embedded DICOM editor** — bundled `dcmjs` tag editor / de-identifier served
  from the dashboard. (`pacs/web/editor/`)
- **Disk-space guard** — refuses ingest below a free-space threshold (`psutil`).
- **PDF / ODF accession attachment** — attach reports to studies by Accession
  Number. (`pacs/ingest.py`)
- Interface redesign and shared Carino navbar/clock.

### Changed
- Description updated from "store-only PACS" to "store-and-reconcile PACS."
- Desktop package license corrected from **MIT** to **AGPL-3.0-or-later** to
  match the repository LICENSE.
- `__version__` bumped `1.0.0` → `1.1.0`.

## [1.0.0] — 2026-07-09

Upstream baseline. Store-and-forward only.

### Added
- **Storage SCP** — accepts C-STORE / C-ECHO and files studies to disk,
  optionally organised by Patient / Study / Series.
- **Auto-send watcher** — forwards new `.dcm` files to N remote nodes via
  C-STORE with per-host retry.
- Single `config.json`, head-less CLI, and local web dashboard.
- Optional TLS transport (TLS 1.2+), `allowed_aets` calling-AE filter.
- Cross-platform desktop packaging (Windows / macOS / Linux) via Electron +
  PyInstaller.
