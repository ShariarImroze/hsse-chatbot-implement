"""Retired legacy migration entry point.

Process Safety in the balanced 400K corpus is selected from authentic USCG NRC
source incident types, not inferred by rewriting four hazard labels in already
generated artifacts. Rebuild from raw sources to preserve quotas and provenance.
"""

from __future__ import annotations


def main() -> None:
    raise SystemExit(
        "This legacy in-place migration is retired. Run "
        "`.venv/bin/python -m incident_pipeline.cli rebuild "
        "--config config/pipeline.json` instead."
    )


if __name__ == "__main__":
    main()
