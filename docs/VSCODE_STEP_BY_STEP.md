# Rebuild the balanced corpus in VS Code

## Goal

Build 400,000 English records from authentic public sources, with exactly
50,000 records in each of eight case types, preserve the nine-column public
schema, retain row-level provenance, and load a verified SQLite/FTS database.

## 1. Open and prepare the project

Open this repository folder in VS Code, then create the local environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Select `.venv/bin/python` with **Python: Select Interpreter**. The Jupyter
extension should use the same kernel.

Checkpoint:

```bash
hsse-data --help
python -m unittest discover -s tests -v
```

## 2. Understand the transformation path

Read these modules in order:

1. `models.py` — exact schema, case-type vocabulary, country normalization;
2. `text.py` — cleanup, language screen, hashes, sampling, release evidence;
3. the source adapter modules — one auditable mapping per publisher;
4. `source_registry.py` — exact source-to-case assignments;
5. `pipeline.py` — deterministic selection, cross-source deduplication, splits;
6. `quality.py` — release-blocking checks;
7. `database.py` — verified atomic SQLite/FTS replacement.

Do not use the retired `reclassify_process_safety.py` migration. Process Safety
is now selected directly from NRC source types and must be rebuilt from source.

## 3. Download and fingerprint authentic sources

Run **Terminal → Run Task → 1. Download public source data**, or:

```bash
hsse-data download
```

The downloader uses retry/backoff for transient API failures, reuses existing
files unless `--force` is supplied, and writes
`data/raw/source_download_manifest.json`.

The manifest must contain 24 files and zero missing entries. It records URLs,
sizes, and SHA-256 hashes for OSHA, NRC, Cal OES, FAA, FDA, CFPB, and Police.uk.
Every subsequent build verifies the stored files against it.

## 4. Build and validate the pilot

Run:

```bash
hsse-data build --config config/pipeline_pilot.json
hsse-data validate --config config/pipeline_pilot.json \
  --report reports/pilot_data_quality_report.json
.venv/bin/python scripts/create_pilot_review.py
```

Expected outputs:

```text
data/processed/pilot_1000.json
data/processed/pilot_1000_with_provenance.jsonl
reports/pilot_label_review.csv
```

The pilot is a bounded smoke-test sample from the leading source rows and
contains 125 records per case type; the 400K build performs the complete-source
sampling used for release. The review sheet contains ten
deterministically sampled rows per type and blank human-review columns. Inspect
description authenticity, source relevance, label fit, severity evidence, and
borderline records before changing any rule.

Important review boundaries:

- Police.uk descriptions are fixed summaries of authentic structured fields;
- CFPB identity-theft rows are unverified consumer allegations;
- NRC rows are unvalidated initial notifications;
- Cal OES no-release/non-material rail events are excluded;
- FAA rows satisfy the asset side, not measured reputation damage;
- FDA recalls proxy operational disruption/loss;
- OSHA Health uses OIICS groups 2–6, with a recorded 2025 fallback where OIICS
  is absent.

## 5. Build, validate, and atomically load the full corpus

After accepting the pilot:

```bash
hsse-data rebuild --config config/pipeline.json
```

The command performs:

1. source-manifest verification;
2. English and 20-word filtering;
3. source relevance and case assignment;
4. source-ID and normalized-text deduplication;
5. deterministic bounded sampling;
6. high-precision near/template filtering where appropriate;
7. exact 50,000-per-type quota enforcement;
8. deterministic 80/10/10 splits;
9. JSON and provenance export;
10. release-blocking quality validation;
11. standalone SQLite/FTS construction and verification;
12. atomic replacement only if the destination WAL is not busy.

Expected outputs:

```text
data/processed/master_400K.json
data/processed/master_400K_with_provenance.jsonl
data/processed/hsse_incidents.sqlite
reports/build_report.json
reports/data_quality_report.json
reports/source_manifest_verification.json
```

The data-quality report must say `PASS`. A high unknown-severity warning is
expected for sources that do not publish defensible outcome severity.

## 6. Regenerate and execute notebooks

```bash
.venv/bin/python scripts/create_master_notebook.py
.venv/bin/python scripts/create_exploratory_notebook.py
.venv/bin/python scripts/execute_notebook.py notebooks/build_master_400k.ipynb
.venv/bin/python scripts/execute_notebook.py notebooks/hsse_exploratory_analysis.ipynb
```

Both notebooks use read-only SQLite access. Confirm that every code cell has an
execution count, no error output exists, the database has 400,000 rows, FTS has
400,000 rows, and every case type has exactly 50,000.

## 7. Before model claims or redistribution

- Create a source-stratified, independently annotated gold set.
- Keep related organizations, facilities, campaigns, and templates in one split
  for leakage-sensitive evaluation.
- Compare random-split with source-held-out performance.
- Return Case No and provenance with retrieval answers.
- Read `DATASET_CARD.md` and `ATTRIBUTION.md`.
- Obtain redistribution review for caller-authored NRC and Cal OES narratives;
  their public archives do not clearly state comprehensive text licences.

Authentic reports can still be inaccurate, alleged, incomplete, or weakly
classified. Preserve that distinction throughout the thesis and chatbot UI.
