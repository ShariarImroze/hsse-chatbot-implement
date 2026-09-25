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

## Local multi-model chatbot

The project includes a local retrieval-augmented chatbot with buttons for Meta
Llama 3.1 8B Instruct and Mistral 7B Instruct v0.3. Each button targets a
separate OpenAI-compatible llama.cpp endpoint. Both use practical Q4_K_M
quantizations for a 16 GB Apple Silicon machine.

The 400K corpus is not copied into a prompt or treated as model memory.
Instead, narrative questions retrieve a small group of relevant records and
supply them to Llama 3 as evidence. This keeps the application local, auditable,
and much lighter than full-model fine-tuning.

Structured dataset questions also use a full-corpus query tool. The selected model translates
the request into a constrained JSON plan, the application validates every field,
operator, category, output column, limit, and offset, and SQLite executes a
parameterized read-only query over all 400,000 source rows. This provides exact
counts, grouped totals, filtered searches, and paginated record lists without
placing the full corpus inside the model context or executing model-written SQL.

### Install the recommended 16 GB setup

Install the project and llama.cpp. The web UI itself has no extra Python UI
dependencies:

```bash
source .venv/bin/activate
python -m pip install -e ".[chatbot]"
brew install llama.cpp
```

The full checkpoint has 8 billion parameters and requires substantially more
memory than the roughly 4.9 GB Q4_K_M GGUF file. The quantized checkpoint is
therefore the recommended local backend for this machine.

### Run

Ensure `data/processed/hsse_incidents.sqlite` exists. In terminal 1, start the
local model server using the command published on the GGUF model card:

```bash
llama-server -hf bartowski/Meta-Llama-3.1-8B-Instruct-GGUF:Q4_K_M --port 8080
```

The optional Mistral endpoint is documented in
[`docs/MODEL_TRAINING.md`](docs/MODEL_TRAINING.md).

The first run downloads the model to the local Hugging Face cache. In terminal 2,
start the loopback-only chatbot UI:

```bash
source .venv/bin/activate
hsse-chatbot --open-browser
```

Open <http://127.0.0.1:7860> if the browser does not open automatically. Both
servers bind to the local machine; no hosted inference provider is used.

Useful options:

```bash
# Override paths, retrieval depth, or llama.cpp server URL.
hsse-chatbot \
  --database data/processed/hsse_incidents.sqlite \
  --user-database data/local/user_cases.sqlite \
  --model-base-url http://127.0.0.1:8080/v1 \
  --top-k 6
```

### Optional full Transformers backend

Use this only on a machine with enough memory for the BF16 model plus inference
overhead. A current Python 3.11 environment is recommended:

```bash
python3.11 -m venv .venv-chat
source .venv-chat/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[chatbot-transformers]"
hf auth login
hsse-chatbot \
  --backend transformers \
  --model-id meta-llama/Meta-Llama-3.1-8B-Instruct \
  --open-browser
```

After the full checkpoint is cached, add `--local-files-only` to prevent model
downloads or network lookups.

### Add cases through chat

Type `add case`. The assistant collects and validates one field at a time:

1. Country
2. Title
3. Description (at least 20 words)
4. Case type (the local model suggests one from the title and description, then asks you
   to confirm or override it)
5. Hazard
6. Hazard type
7. Actual severity
8. Potential severity

Type `back` to revise the previous field, `cancel` to discard the draft, and
`save` after reviewing the summary. New records are written to
`data/local/user_cases.sqlite` and become searchable immediately. The canonical
400K database remains read-only and reproducible.

Unfinished drafts are stored locally for the browser session and restored after
an application or browser restart. Clearing the conversation or typing `cancel`
deletes the draft. Asking whether a case was recorded reports its draft/saved
status without restarting or advancing the wizard.

Completed exchanges are also stored locally in `data/local/user_cases.sqlite`,
including the selected model ID and response latency.
Clearing the browser starts a new conversation without deleting the archived
transcript. The optional local Codex plugin in `plugins/hsse-chat-history/`
provides read-only tools to list, retrieve, and search these conversations for
chatbot evaluation. It does not expose the 400K source database.

At the case-type step, reply `yes` or `suggested` to accept the recommendation.
To correct it, enter a different menu number, the exact case-type name, or a
sentence such as `No, this should be Occupational Safety`. The user's confirmed
choice is always the value saved with the case.

### Generate charts through chat

Ask for a chart, graph, plot, histogram, or visualization. Llama 3 converts the
request into a constrained chart plan; the application validates the plan and
runs an allowlisted aggregation against the complete local SQLite corpus. Raw
model output is never executed as SQL or JavaScript.

Examples:

```text
Show a bar chart of incidents by case type
Graph the top 10 countries by incident count
Plot case type by country as a stacked chart
Show a line chart of incidents by event year
Create a log graph of description word counts
```

Charts are rendered locally without a CDN and include an expandable exact-value
table. Supported dimensions are case type, country, hazard type, actual and
potential severity, source, dataset split, event year, and description word
count. Corpus charts use the reproducible 400K source database; locally entered
cases remain searchable in chat but are not mixed into source-corpus totals.

### Query the complete dataset through chat

Ordinary structured questions use the same complete, read-only source database:

```text
How many Process Safety incidents are there?
Break down incidents by case type
Count Severe incidents by country
List 20 Process Safety cases with case number, title, and description
Show the next page starting at offset 20
Find cases containing pump, seal, and fire
```

Lists are limited to 50 records per response and report the complete match count.
Use the returned offset to page through larger result sets. Narrative and
similarity questions continue to use bounded FTS retrieval because hundreds of
thousands of descriptions cannot fit inside one model prompt.

### Local architecture

```text
Browser on 127.0.0.1
        |
        v
Python standard-library web server <------> local llama.cpp model server
        |
        +-- deterministic case-entry wizard --> data/local/user_cases.sqlite
        |
        +-- SQLite FTS5 retrieval ------------> 400K corpus + local cases
        |
        +-- validated chart plan + aggregation -> local SVG/CSS charts
        |
        +-- retrieved evidence + chat history -> Meta Llama 3 8B -> answer
```

## Important qualification

Authentic report does not mean verified fact. CFPB complaints are consumer
allegations, NRC rows are initial caller reports, and several labels are
deterministic weak labels. Equal quotas are construction choices, not estimates
of real-world prevalence. Review [ATTRIBUTION.md](ATTRIBUTION.md) before
redistributing narrative text; some official archives do not state an explicit
open-data licence.
