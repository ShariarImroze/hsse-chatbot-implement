"""Command-line interface for the balanced incident-data pipeline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .database import load_sqlite
from .download import download_all, verify_source_manifest
from .pipeline import build_balanced_dataset
from .quality import validate_dataset
from .source_registry import candidate_jobs


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RAW = PROJECT_ROOT / "data" / "raw"
DEFAULT_PROCESSED = PROJECT_ROOT / "data" / "processed"
DEFAULT_REPORTS = PROJECT_ROOT / "reports"
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "pipeline.json"


def _load_config(path: Path) -> dict[str, object]:
    with path.open(encoding="utf-8") as stream:
        config = json.load(stream)
    quotas = {
        key: int(value)
        for key, value in dict(config["case_type_quotas"]).items()
    }
    if int(config["dataset_size"]) != sum(quotas.values()):
        raise ValueError("dataset_size must equal the sum of case_type_quotas")
    return config


def _dataset_paths(
    config: dict[str, object],
    output_directory: Path,
) -> tuple[Path, Path]:
    dataset_name = str(config["dataset_name"])
    return (
        output_directory / f"{dataset_name}.json",
        output_directory / f"{dataset_name}_with_provenance.jsonl",
    )


def _default_quality_report(config: dict[str, object]) -> Path:
    dataset_name = str(config["dataset_name"])
    if dataset_name == "master_400K":
        return DEFAULT_REPORTS / "data_quality_report.json"
    if dataset_name == "pilot_1000":
        return DEFAULT_REPORTS / "pilot_data_quality_report.json"
    return DEFAULT_REPORTS / f"{dataset_name}_data_quality_report.json"


def _default_database(config: dict[str, object]) -> Path:
    dataset_name = str(config["dataset_name"])
    if dataset_name == "master_400K":
        return DEFAULT_PROCESSED / "hsse_incidents.sqlite"
    return DEFAULT_PROCESSED / f"{dataset_name}.sqlite"


def _build_from_config(
    config_path: Path,
    raw_directory: Path,
    output_directory: Path,
) -> dict[str, object]:
    manifest_report = verify_source_manifest(raw_directory)
    manifest_report_path = DEFAULT_REPORTS / "source_manifest_verification.json"
    manifest_report_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_report_path.open("w", encoding="utf-8") as stream:
        json.dump(manifest_report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    if manifest_report["status"] != "PASS":
        raise ValueError(
            "Raw-source manifest verification failed; see "
            f"{manifest_report_path}"
        )

    config = _load_config(config_path)
    quotas = {
        key: int(value)
        for key, value in dict(config["case_type_quotas"]).items()
    }
    assignments = {
        key: str(value)
        for key, value in dict(config["case_type_sources"]).items()
    }
    dataset_name = str(config["dataset_name"])
    build_report_name = (
        "build_report.json"
        if dataset_name == "master_400K"
        else f"{dataset_name}_build_report.json"
    )
    return build_balanced_dataset(
        jobs=candidate_jobs(raw_directory, assignments),
        output_directory=output_directory,
        dataset_name=dataset_name,
        case_type_quotas=quotas,
        seed=int(config["random_seed"]),
        minimum_words=int(config["minimum_description_words"]),
        similarity_threshold=float(config["near_duplicate_similarity"]),
        maximum_rows_per_source={
            key: int(value)
            for key, value in dict(
                config.get("maximum_rows_per_source", {})
            ).items()
        },
        split_ratios={
            key: float(value)
            for key, value in dict(config["splits"]).items()
        },
        report_path=DEFAULT_REPORTS / build_report_name,
    )


def _validate_from_config(
    config_path: Path,
    output_directory: Path,
    report_path: Path,
) -> dict[str, object]:
    config = _load_config(config_path)
    public_path, provenance_path = _dataset_paths(config, output_directory)
    return validate_dataset(
        public_path,
        provenance_path,
        report_path,
        expected_case_type_quotas={
            key: int(value)
            for key, value in dict(config["case_type_quotas"]).items()
        },
        minimum_description_words=int(config["minimum_description_words"]),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build and validate the balanced 400K English incident corpus"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    download_parser = subparsers.add_parser(
        "download", help="Download authentic public source files"
    )
    download_parser.add_argument("--raw-directory", default=str(DEFAULT_RAW))
    download_parser.add_argument("--force", action="store_true")

    build_parser_command = subparsers.add_parser(
        "build", help="Normalize, deduplicate, balance, and export JSON"
    )
    build_parser_command.add_argument("--config", default=str(DEFAULT_CONFIG))
    build_parser_command.add_argument(
        "--raw-directory", default=str(DEFAULT_RAW)
    )
    build_parser_command.add_argument("--output", default=str(DEFAULT_PROCESSED))

    database_parser = subparsers.add_parser(
        "load-sqlite", help="Load provenance JSONL into SQLite with FTS"
    )
    database_parser.add_argument(
        "--input",
        default=str(
            DEFAULT_PROCESSED / "master_400K_with_provenance.jsonl"
        ),
    )
    database_parser.add_argument(
        "--database",
        default=str(DEFAULT_PROCESSED / "hsse_incidents.sqlite"),
    )

    validate_parser = subparsers.add_parser(
        "validate", help="Run release-blocking data-quality checks"
    )
    validate_parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    validate_parser.add_argument("--output", default=str(DEFAULT_PROCESSED))
    validate_parser.add_argument(
        "--report",
        default=None,
        help="Defaults to a dataset-specific report path derived from --config",
    )

    rebuild_parser = subparsers.add_parser(
        "rebuild",
        help="Build JSON, validate it, and replace the SQLite database",
    )
    rebuild_parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    rebuild_parser.add_argument(
        "--raw-directory", default=str(DEFAULT_RAW)
    )
    rebuild_parser.add_argument("--output", default=str(DEFAULT_PROCESSED))
    rebuild_parser.add_argument(
        "--report",
        default=None,
        help="Defaults to a dataset-specific report path derived from --config",
    )
    rebuild_parser.add_argument(
        "--database",
        default=None,
        help="Defaults to the canonical DB for master_400K, otherwise <dataset>.sqlite",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "download":
        download_all(Path(args.raw_directory), force=args.force)
    elif args.command == "build":
        _build_from_config(
            Path(args.config),
            Path(args.raw_directory),
            Path(args.output),
        )
    elif args.command == "load-sqlite":
        load_sqlite(Path(args.input), Path(args.database))
    elif args.command == "validate":
        config_path = Path(args.config)
        config = _load_config(config_path)
        _validate_from_config(
            config_path,
            Path(args.output),
            Path(args.report) if args.report else _default_quality_report(config),
        )
    elif args.command == "rebuild":
        config_path = Path(args.config)
        output_directory = Path(args.output)
        config = _load_config(config_path)
        _build_from_config(
            config_path,
            Path(args.raw_directory),
            output_directory,
        )
        _validate_from_config(
            config_path,
            output_directory,
            Path(args.report) if args.report else _default_quality_report(config),
        )
        _, provenance_path = _dataset_paths(config, output_directory)
        load_sqlite(
            provenance_path,
            Path(args.database) if args.database else _default_database(config),
        )
    else:
        parser.error(f"Unknown command: {args.command}")


if __name__ == "__main__":
    main()
