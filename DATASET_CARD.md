# Dataset card: balanced 400K English incident corpus

## Summary

The corpus contains 400,000 independently identified source records: exactly
50,000 in each of eight case types. It supports classification, retrieval,
data-quality research, and early incident-chatbot experiments.

It is not suitable for estimating incident prevalence, regulatory compliance,
country performance, or causal risk. Source mandates, years, coverage, and
selection mechanisms differ substantially, and equal class quotas are imposed
by design.

## Unit of analysis and schema

One row represents one retained source record after language, length, relevance,
identity, and duplicate checks. The public schema is exactly:

1. `Case No`
2. `Country`
3. `Title`
4. `Description`
5. `Case Type`
6. `Hazard`
7. `Hazard Type`
8. `Actual Severity`
9. `Potential Severity`

The provenance sidecar retains the native source ID, source URL and terms,
event date, raw classification evidence, deterministic label methods, normalized
text hash, sampling rank, and split.

## Composition and classification boundaries

| Case type | Rule and source |
|---|---|
| Occupational Safety | OSHA ITA rows in OIICS Nature major group 1 (traumatic injuries/effects). |
| Occupational Health | OSHA ITA rows in OIICS Nature major groups 2–6. When the 2025 release lacks OIICS, source `type_of_incident` values 2–6 are used and explicitly recorded as fallback provenance. |
| Process Safety | USCG NRC initial reports whose source incident type is `FIXED`, `STORAGE TANK`, `PIPELINE`, or `PLATFORM`; explicit potential/no-release reports are excluded. |
| Environment | Cal OES spill notifications with environmental/release evidence; explicit no-release and non-material railroad notifications are excluded. |
| Asset and Reputation Damage/Loss | FAA Service Difficulty Reports with authentic discrepancy text. This operationalizes the asset-loss side only; it does not measure reputation loss. |
| Operational Loss | FDA enforcement recall reports using the complete source recall reason, product description, lot/code, and additional code fields without truncation; source classification is retained in provenance. |
| Information Security | CFPB complaints in documented identity-theft classifications. Explicit identity-theft source classifications qualify directly; the contextual credit-report classification additionally requires identity-theft wording in the published narrative. These are allegations, not verified cyber intrusions. |
| Physical Security | Police.uk street-crime records restricted to bicycle theft, burglary, criminal damage/arson, weapons, robbery, theft-from-person, and vehicle-crime categories. Description is a fixed rendering of native fields because no narrative is published. |

These definitions were intentionally approved as broad source-to-case mappings.
They remain weak labels rather than expert-adjudicated ground truth.

## Authenticity and language policy

- Every row has a native source identifier and URL.
- No AI system generates incident reports or fills missing narratives.
- Source-authored narratives are preserved after Unicode/whitespace cleanup.
- Deterministic source-field renderings do not invent event facts.
- Every description contains at least 20 English words under the project screen.
- Exact normalized description duplicates are prohibited globally.
- Source-specific high-precision template/near-duplicate controls are applied
  where useful and recorded in the build report.

The English detector is a conservative heuristic based on Latin-character share
and English function words. It is not a language-identification model and can
make edge-case errors.

## Sampling and splits

Eligible candidates receive a deterministic SHA-256 sampling rank based on the
fixed seed, source, and native ID. The smallest ranks are selected after global
deduplication until each quota is met. Case-number hashes assign approximately
80% train, 10% validation, and 10% test.

Random deterministic splits do not guarantee independence between organizations,
facilities, campaigns, product families, or narrative templates. Source-held-out
and entity/time-grouped evaluations are recommended.

## Severity and hazard labels

Hazard and severity rules use source classification/consequence fields and
documented deterministic mappings. `Unknown` is retained when the source lacks
defensible evidence. Potential severity is a weak consequence rule and is never
below known actual severity.

Do not interpret an `Unknown` actual severity as low severity. Do not use these
weak labels as the sole evaluation truth. Create a source-stratified gold sample
with a written codebook, at least two annotators, adjudication, and agreement
measurement before research claims.

## Release-blocking quality checks

The validator requires:

- exact 400,000 rows and exact 50,000 quotas;
- exact nine-field public schema and complete required values;
- unique case numbers, text hashes, and `(source, source_record_id)` keys;
- exact public/provenance row correspondence;
- complete raw-label and label-method provenance;
- canonical country labels;
- accepted case, hazard, severity, and split domains;
- at least 20 words and an English-screen pass for every description;
- zero exact normalized narrative duplicates;
- source-to-case mappings and NRC Process Safety source types;
- potential severity not below actual severity;
- SQLite `quick_check`, row coverage, and FTS coverage before replacement;
- raw-file size and SHA-256 agreement with the 24-file source manifest.

## Principal limitations

- OSHA OIICS fields are machine-coded predictions supplied in the official
  release; 2025 health fallback is coarser.
- NRC explicitly warns that initial caller reports are not validated or
  investigated by an agency.
- Cal OES and NRC public archives do not clearly state a comprehensive licence
  for caller-authored narrative text.
- FAA discrepancies prove asset/service difficulty, not reputation damage.
- FDA recall records represent enforcement-report events, not a complete measure
  of business interruption or monetary loss.
- CFPB publishes unverified opt-in allegations, with suspected template usage in
  parts of the complaint system despite project deduplication.
- Police.uk descriptions are deterministic structured summaries, and crime
  location is anonymized/approximate.
- Geography and dates reflect source scope and precision, not uniform global
  coverage.

## Redistribution

The repository code is MIT-licensed. Source records retain their own terms.
Consult [ATTRIBUTION.md](ATTRIBUTION.md) and obtain legal review before publishing
the combined narrative corpus. This dataset card is methodological information,
not legal advice.
