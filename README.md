# Balanced 400K incident data pipeline

This project builds an English-language research corpus of exactly **400,000
authentic incident and complaint records**. It contains 50,000 records in each
of eight mutually exclusive case types; no type exceeds 100,000.

No incident description is produced by an AI model. Descriptions are either
source-authored narrative text or a deterministic rendering of fields in the
official source record. The latter is used for Police.uk because that bulk
dataset does not publish narrative remarks.

## Case types and sources

| Case type | Records | Authentic source |
|---|---:|---|
| Occupational Safety | 50,000 | OSHA ITA case detail |
| Occupational Health | 50,000 | OSHA ITA case detail |
| Process Safety | 50,000 | USCG National Response Center |
| Environment | 50,000 | California OES spill reports |
| Asset and Reputation Damage/Loss | 50,000 | FAA Service Difficulty Reports |
| Operational Loss | 50,000 | FDA recall enforcement reports |
| Information Security | 50,000 | CFPB identity-theft complaint narratives |
| Physical Security | 50,000 | Police.uk street-crime records |
| **Total** | **400,000** | |

The combined Asset and Reputation label is satisfied through documented asset
damage/loss reports. It must not be interpreted as measured reputation loss.
See [DATASET_CARD.md](DATASET_CARD.md) for every classification boundary.

## Public schema

The model-facing JSON retains the current nine columns, in this order:

```text
Case No
Country
Title
Description
Case Type
Hazard
Hazard Type
Actual Severity
Potential Severity
```

The provenance JSONL and SQLite table add source URL, native record ID, event
date, raw source labels, label methods, text hash, sampling rank, and dataset
split.

## Rebuild

Create the environment and install runtime plus notebook dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

Then run:

```bash
hsse-data download
hsse-data build --config config/pipeline_pilot.json
hsse-data validate --config config/pipeline_pilot.json \
  --report reports/pilot_data_quality_report.json
hsse-data rebuild --config config/pipeline.json
```

`download` reuses existing files unless `--force` is supplied and writes a
24-file SHA-256 manifest. Every build verifies that manifest before reading raw
data. `rebuild` writes JSON/provenance, blocks on quality failures, and only then
atomically replaces SQLite.

## Outputs

```text
data/processed/pilot_1000.json
data/processed/pilot_1000_with_provenance.jsonl
data/processed/master_400K.json
data/processed/master_400K_with_provenance.jsonl
data/processed/hsse_incidents.sqlite
reports/build_report.json
reports/pilot_1000_build_report.json
reports/data_quality_report.json
reports/pilot_data_quality_report.json
reports/source_manifest_verification.json
reports/pilot_label_review.csv
```

SQLite includes indexes on common filters and an FTS5 index over titles,
descriptions, and hazards.

## Notebooks

All project-owned notebooks live in `notebooks/`:

- `build_master_400k.ipynb` reconciles configuration, sources, reports, and the
  final database.
- `hsse_exploratory_analysis.ipynb` is a separate read-only exploration of
  `hsse_incidents.sqlite`.

Regenerate and execute them with:

```bash
.venv/bin/python scripts/create_master_notebook.py
.venv/bin/python scripts/create_exploratory_notebook.py
.venv/bin/python scripts/execute_notebook.py notebooks/build_master_400k.ipynb
.venv/bin/python scripts/execute_notebook.py notebooks/hsse_exploratory_analysis.ipynb
```

## Important qualification

Authentic report does not mean verified fact. CFPB complaints are consumer
allegations, NRC rows are initial caller reports, and several labels are
deterministic weak labels. Equal quotas are construction choices, not estimates
of real-world prevalence. Review [ATTRIBUTION.md](ATTRIBUTION.md) before
redistributing narrative text; some official archives do not state an explicit
open-data licence.
