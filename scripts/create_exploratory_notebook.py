"""Generate a reproducible exploratory analysis of hsse_incidents.sqlite."""

from __future__ import annotations

import sys
from pathlib import Path
from textwrap import dedent

import nbformat as nbf


def markdown(source: str):
    return nbf.v4.new_markdown_cell(dedent(source).strip())


def code(source: str):
    return nbf.v4.new_code_cell(dedent(source).strip())


def create_notebook(output_path: Path) -> None:
    cells = [
        markdown(
            """
            # Exploratory analysis — HSSE incidents SQLite

            This separate, read-only notebook profiles
            `data/processed/hsse_incidents.sqlite`. It focuses on corpus composition,
            label coverage, source effects, dates, countries, text length, model splits,
            and full-text search. All headline values are calculated from the database at
            execution time rather than copied into notebook prose.
            """
        ),
        markdown(
            """
            ## Analytical framing

            The database is a deliberately balanced research corpus, not a prevalence
            sample. Each case type has an equal quota, and each type is tied to a specific
            authentic source or documented source rule. Comparisons across labels therefore
            describe this dataset—not population incident rates. Several labels and severity
            values are deterministic weak labels and require human validation before being
            treated as ground truth.
            """
        ),
        code(
            """
            from __future__ import annotations

            import os
            import sqlite3
            from pathlib import Path

            import matplotlib.pyplot as plt
            import numpy as np
            import pandas as pd
            from IPython.display import display

            plt.style.use("seaborn-v0_8-whitegrid")
            pd.set_option("display.max_colwidth", 140)


            def locate_project_root(start: Path) -> Path:
                for candidate in [start, *start.parents]:
                    if (candidate / "src" / "incident_pipeline").is_dir() and (
                        candidate / "config" / "pipeline.json"
                    ).is_file():
                        return candidate
                raise FileNotFoundError("Could not locate the project root")


            PROJECT_ROOT = locate_project_root(Path.cwd().resolve())
            DB_PATH = Path(
                os.environ.get(
                    "HSSE_DATABASE_PATH",
                    str(
                        PROJECT_ROOT
                        / "data"
                        / "processed"
                        / "hsse_incidents.sqlite"
                    ),
                )
            ).resolve()
            connection = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
            print(f"Read-only database: {DB_PATH}")
            """
        ),
        markdown("## 1. Database structure and integrity"),
        code(
            """
            objects = pd.read_sql_query(
                '''
                SELECT type, name
                FROM sqlite_master
                WHERE name NOT LIKE 'sqlite_%'
                ORDER BY type, name
                ''',
                connection,
            )
            schema = pd.read_sql_query("PRAGMA table_info(incidents)", connection)
            display(objects)
            display(schema[["cid", "name", "type", "notnull", "pk"]])
            quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
            print("SQLite quick_check:", quick_check)
            assert quick_check == "ok"
            """
        ),
        code(
            """
            integrity = pd.read_sql_query(
                '''
                SELECT
                    COUNT(*) AS rows,
                    COUNT(DISTINCT case_no) AS distinct_case_no,
                    COUNT(DISTINCT text_hash) AS distinct_text_hash,
                    COUNT(DISTINCT source || char(31) || source_record_id)
                        AS distinct_source_record,
                    SUM(TRIM(description) = '') AS blank_description,
                    SUM(TRIM(country) = '') AS blank_country,
                    SUM(TRIM(event_date) = '') AS blank_event_date
                FROM incidents
                ''',
                connection,
            )
            fts_rows = connection.execute("SELECT COUNT(*) FROM incidents_fts").fetchone()[0]
            integrity["fts_rows"] = fts_rows
            display(integrity.T.rename(columns={0: "value"}))
            assert integrity.loc[0, "rows"] == 400_000
            assert integrity.loc[0, "rows"] == integrity.loc[0, "distinct_case_no"]
            assert integrity.loc[0, "rows"] == integrity.loc[0, "distinct_text_hash"]
            assert integrity.loc[0, "rows"] == integrity.loc[0, "distinct_source_record"]
            assert fts_rows == integrity.loc[0, "rows"]
            """
        ),
        markdown("## 2. Case-type balance and source dependence"),
        code(
            """
            case_counts = pd.read_sql_query(
                '''
                SELECT case_type, COUNT(*) AS records
                FROM incidents
                GROUP BY case_type
                ORDER BY records DESC, case_type
                ''',
                connection,
            )
            case_counts["share_pct"] = 100 * case_counts["records"] / case_counts["records"].sum()
            display(case_counts)
            assert len(case_counts) == 8
            assert set(case_counts["records"]) == {50_000}
            assert case_counts["records"].sum() == 400_000

            ax = case_counts.sort_values("records").plot.barh(
                x="case_type", y="records", legend=False, figsize=(9, 5), color="#4472C4"
            )
            ax.set(xlabel="Records", ylabel="", title="Exactly balanced case-type quotas")
            ax.bar_label(ax.containers[0], fmt="%d", padding=3)
            plt.tight_layout()
            plt.show()
            """
        ),
        code(
            """
            source_case = pd.read_sql_query(
                '''
                SELECT source, case_type, COUNT(*) AS records
                FROM incidents
                GROUP BY source, case_type
                ''',
                connection,
            )
            source_case_pivot = source_case.pivot_table(
                index="source", columns="case_type", values="records", fill_value=0
            )
            source_case_pct = source_case_pivot.div(source_case_pivot.sum(axis=1), axis=0) * 100
            display(source_case_pivot)

            fig, ax = plt.subplots(figsize=(12, 4.5))
            image = ax.imshow(source_case_pct, aspect="auto", cmap="Blues", vmin=0, vmax=100)
            ax.set_xticks(range(len(source_case_pct.columns)), source_case_pct.columns, rotation=40, ha="right")
            ax.set_yticks(range(len(source_case_pct.index)), source_case_pct.index)
            for i in range(source_case_pct.shape[0]):
                for j in range(source_case_pct.shape[1]):
                    value = source_case_pct.iloc[i, j]
                    if value:
                        ax.text(j, i, f"{value:.0f}%", ha="center", va="center", color="black")
            fig.colorbar(image, ax=ax, label="Within-source share (%)")
            ax.set_title("Case type is structurally tied to source selection")
            plt.tight_layout()
            plt.show()
            """
        ),
        markdown("## 3. Hazard and severity labels"),
        code(
            """
            hazard_counts = pd.read_sql_query(
                '''
                SELECT hazard_type, COUNT(*) AS records
                FROM incidents
                GROUP BY hazard_type
                ORDER BY records DESC
                ''',
                connection,
            )
            hazard_counts["share_pct"] = 100 * hazard_counts["records"] / hazard_counts["records"].sum()
            display(hazard_counts)

            top_hazards = hazard_counts.head(15).sort_values("records")
            ax = top_hazards.plot.barh(
                x="hazard_type", y="records", legend=False, figsize=(9, 6), color="#70AD47"
            )
            ax.set(xlabel="Records", ylabel="", title="Top 15 hazard types")
            plt.tight_layout()
            plt.show()
            """
        ),
        code(
            """
            severity = pd.read_sql_query(
                '''
                SELECT actual_severity, potential_severity, COUNT(*) AS records
                FROM incidents
                GROUP BY actual_severity, potential_severity
                ''',
                connection,
            )
            severity_matrix = severity.pivot_table(
                index="actual_severity",
                columns="potential_severity",
                values="records",
                fill_value=0,
            ).reindex(index=["Low", "Medium", "Severe", "Unknown"],
                      columns=["Low", "Medium", "Severe", "Unknown"], fill_value=0)
            display(severity_matrix)

            fig, ax = plt.subplots(figsize=(6.5, 5))
            image = ax.imshow(severity_matrix, cmap="YlOrRd")
            ax.set_xticks(range(4), severity_matrix.columns)
            ax.set_yticks(range(4), severity_matrix.index)
            ax.set(xlabel="Potential severity", ylabel="Actual severity",
                   title="Actual vs potential severity")
            for i in range(4):
                for j in range(4):
                    ax.text(j, i, f"{severity_matrix.iloc[i, j]:,}", ha="center", va="center")
            fig.colorbar(image, ax=ax, label="Records")
            plt.tight_layout()
            plt.show()
            """
        ),
        markdown("## 4. Geographic composition"),
        code(
            """
            country_counts = pd.read_sql_query(
                '''
                SELECT country, COUNT(*) AS records
                FROM incidents
                GROUP BY country
                ORDER BY records DESC, country
                ''',
                connection,
            )
            country_counts["share_pct"] = 100 * country_counts["records"] / country_counts["records"].sum()
            display(country_counts.head(20))

            top_countries = country_counts.head(12).sort_values("records")
            ax = top_countries.plot.barh(
                x="country", y="records", legend=False, figsize=(9, 5), color="#5B9BD5"
            )
            ax.set(xlabel="Records", ylabel="", title="Top countries (source-driven)")
            plt.tight_layout()
            plt.show()
            """
        ),
        markdown(
            """
            ## 5. Event-date coverage

            Date formats differ by source. The parser below is source-aware and retains
            Police.uk monthly precision rather than inventing an exact day for analysis.
            """
        ),
        code(
            """
            dates = pd.read_sql_query("SELECT source, event_date FROM incidents", connection)
            formats = {
                "CALOES_SPILL": "%Y-%m-%d",
                "FAA_SDR": "%m/%d/%Y",
                "FDA_RES": "%Y%m%d",
                "OSHA_ITA_CASE_DETAIL": "%Y-%m-%d",
                "POLICE_UK": "%Y-%m",
                "USCG_NRC": "%m/%d/%Y %H:%M",
            }
            parsed_parts = []
            for source, group in dates.groupby("source", sort=False):
                values = group["event_date"]
                if source == "CFPB_CONSUMER_COMPLAINT":
                    parsed = pd.to_datetime(values, errors="coerce", utc=True).dt.tz_localize(None)
                else:
                    parsed = pd.to_datetime(values, format=formats[source], errors="coerce")
                part = group.copy()
                part["parsed_date"] = parsed
                part["date_precision"] = "month" if source == "POLICE_UK" else "day"
                parsed_parts.append(part)
            parsed_dates = pd.concat(parsed_parts, ignore_index=True)
            parsed_dates["year"] = parsed_dates["parsed_date"].dt.year.astype("Int64")

            date_coverage = parsed_dates.groupby("source").agg(
                rows=("event_date", "size"),
                parsed=("parsed_date", "count"),
                minimum=("parsed_date", "min"),
                maximum=("parsed_date", "max"),
            )
            date_coverage["parse_rate_pct"] = 100 * date_coverage["parsed"] / date_coverage["rows"]
            display(date_coverage)
            assert date_coverage["parsed"].sum() == len(parsed_dates)
            today = pd.Timestamp.now(tz="UTC").tz_localize(None).normalize()
            assert not (parsed_dates["parsed_date"] > today).any()
            """
        ),
        code(
            """
            yearly = (
                parsed_dates.dropna(subset=["year"])
                .groupby(["year", "source"])
                .size()
                .unstack(fill_value=0)
            )
            ax = yearly.plot(figsize=(11, 5), marker="o", linewidth=1.5)
            ax.set(xlabel="Year", ylabel="Records", title="Yearly counts by source")
            ax.legend(title="Source", bbox_to_anchor=(1.02, 1), loc="upper left")
            plt.tight_layout()
            plt.show()
            """
        ),
        markdown("## 6. Description length and repeated-title structure"),
        code(
            """
            text_profile = pd.read_sql_query(
                '''
                SELECT source,
                       COUNT(*) AS records,
                       ROUND(AVG(LENGTH(description)), 1) AS mean_chars,
                       MIN(LENGTH(description)) AS min_chars,
                       MAX(LENGTH(description)) AS max_chars,
                       COUNT(DISTINCT title) AS distinct_titles
                FROM incidents
                GROUP BY source
                ORDER BY records DESC
                ''',
                connection,
            )
            display(text_profile)

            ax = text_profile.sort_values("mean_chars").plot.barh(
                x="source", y="mean_chars", legend=False, figsize=(9, 4.5), color="#ED7D31"
            )
            ax.set(xlabel="Mean characters", ylabel="", title="Mean description length by source")
            plt.tight_layout()
            plt.show()
            """
        ),
        markdown("## 7. Train/validation/test balance"),
        code(
            """
            splits = pd.read_sql_query(
                '''
                SELECT case_type, dataset_split, COUNT(*) AS records
                FROM incidents
                GROUP BY case_type, dataset_split
                ''',
                connection,
            )
            split_pivot = splits.pivot(index="case_type", columns="dataset_split", values="records")
            split_pct = split_pivot.div(split_pivot.sum(axis=1), axis=0) * 100
            display(split_pivot)
            display(split_pct.round(2))

            target = pd.Series({"train": 80.0, "validation": 10.0, "test": 10.0})
            display((split_pct[target.index] - target).round(2).rename_axis(columns="Delta from target (pp)"))
            """
        ),
        markdown("## 8. Bounded full-text-search example"),
        code(
            """
            query = "fire OR explosion"
            fts_results = pd.read_sql_query(
                '''
                SELECT i.case_no, i.case_type, i.source, i.title,
                       snippet(incidents_fts, 2, '[', ']', ' … ', 18) AS match
                FROM incidents_fts
                JOIN incidents AS i ON i.rowid = incidents_fts.rowid
                WHERE incidents_fts MATCH ?
                LIMIT 12
                ''',
                connection,
                params=(query,),
            )
            display(fts_results)
            """
        ),
        markdown(
            """
            ## Takeaways and cautions

            1. The corpus is exactly balanced by design; source and case type are strongly
               coupled, so a random split can reward source-style recognition.
            2. Descriptions are authentic reports or fixed source-field renderings, but
               complaint and initial-report sources are not necessarily independently verified.
            3. Unknown severity is informative missingness, not a low-severity label.
            4. Date coverage and precision differ materially by source.
            5. Geographic comparisons are dominated by source scope.
            6. Before performance claims, create a human-reviewed, source-stratified gold
               set and test source-held-out as well as random-split generalisation.
            """
        ),
        code(
            """
            connection.close()
            print("Closed the read-only SQLite connection.")
            """
        ),
    ]

    notebook = nbf.v4.new_notebook(
        cells=cells,
        metadata={
            "kernelspec": {
                "display_name": "Python 3 (.venv)",
                "language": "python",
                "name": "python3",
            },
            "language_info": {
                "name": "python",
                "version": (
                    f"{sys.version_info.major}."
                    f"{sys.version_info.minor}."
                    f"{sys.version_info.micro}"
                ),
            },
        },
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    nbf.write(notebook, output_path)


if __name__ == "__main__":
    destination = (
        Path(sys.argv[1])
        if len(sys.argv) > 1
        else Path("notebooks/hsse_exploratory_analysis.ipynb")
    )
    create_notebook(destination.resolve())
    print(f"Created {destination.resolve()}")
