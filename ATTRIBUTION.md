# Source attribution and redistribution notes

The pipeline combines records from seven official publishers. Raw-file URLs,
retrieval paths, byte sizes, and SHA-256 hashes are recorded in
`data/raw/source_download_manifest.json`. The notes below summarize source use;
they are not legal advice.

## OSHA Injury Tracking Application case detail

- Publisher: United States Occupational Safety and Health Administration
- Landing page: https://www.osha.gov/itadata
- Use: Occupational Safety and Occupational Health narratives, dates, outcomes,
  and OIICS/source classifications.
- Qualification: employer-submitted reports cover qualifying establishments;
  official OIICS prediction fields are machine coded. United States government
  data are publicly accessible, but review agency terms before redistribution.

## USCG National Response Center

- Publisher: United States Coast Guard National Response Center
- Landing page: https://nrc.uscg.mil/
- Use: Process Safety reports for fixed facilities, storage tanks, pipelines,
  and platforms.
- Qualification: NRC states these are initial caller reports that have not been
  validated or investigated. The FOIA download page does not state an explicit
  dataset licence for caller-authored text; preserve attribution and obtain
  redistribution review.

## California OES hazardous-material spill reports

- Publisher: California Governor's Office of Emergency Services
- Landing page: https://www.caloes.ca.gov/office-of-the-director/operations/response-operations/fire-rescue/hazardous-materials/spill-release-reporting/
- Use: Environment incident descriptions and structured spill/consequence fields
  from annual 2016–2024 workbooks.
- Qualification: the archive is publicly downloadable, but no comprehensive
  dataset licence for caller-authored narrative text is stated on the landing
  page. Preserve attribution and review reuse terms before redistribution.

## FAA Service Difficulty Reports

- Publisher: United States Federal Aviation Administration
- Landing page: https://www.faa.gov/av-info/download_SDR
- Use: Asset and Reputation Damage/Loss records, specifically the asset/service-
  difficulty side of that combined category.
- Qualification: the source does not establish measured reputation damage.
  United States government data are publicly accessible; source terms apply.

## FDA recall enforcement reports through openFDA

- Publisher: United States Food and Drug Administration
- Landing page: https://open.fda.gov/apis/downloads/
- Licence: CC0 1.0 Universal unless otherwise noted by openFDA.
- Use: Operational Loss records from food, drug, and device recall enforcement
  reports. Descriptions deterministically join the source reason, product, and
  classification fields without adding event facts.

## CFPB Consumer Complaint Database

- Publisher: United States Consumer Financial Protection Bureau
- Landing page: https://www.consumerfinance.gov/data-research/consumer-complaints/
- Licence: CC0 1.0 Universal.
- Use: Information Security identity-theft complaints with published opt-in,
  PII-scrubbed consumer narratives.
- Qualification: CFPB does not verify the truth or accuracy of complaint
  allegations, and published narratives may contain repeated/template language.

## Police.uk open crime data

- Publisher: UK Home Office / Police.uk
- Landing page: https://data.police.uk/data/
- Licence: Open Government Licence v3.0.
- Use: Physical Security street-crime records from a documented Metropolitan
  Police export. Native fields are rendered into a fixed non-AI description
  because the bulk street files do not include narrative remarks.
- Qualification: published coordinates/locations are anonymized or approximate.

## Combined output

The code is MIT-licensed; that does not replace source licences or rights in
source-authored text. A public release should retain record-level source URLs and
this attribution, comply with OGL/CC0 conditions, and obtain review for NRC and
Cal OES narrative redistribution. Distributing only code, manifests, and
rebuild instructions can reduce—but does not itself resolve—those questions.
