"""Reproducible downloads for the authentic public source files."""

from __future__ import annotations

import hashlib
import http.client
import json
import math
import os
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any


USER_AGENT = "hsse-incident-data-pipeline/0.2 source-audit"
RETRYABLE_HTTP_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
DEFAULT_DOWNLOAD_ATTEMPTS = 5
DEFAULT_INITIAL_BACKOFF_SECONDS = 2.0
MAXIMUM_RETRY_DELAY_SECONDS = 300.0


def _cfpb_url(date_min: str, date_max: str) -> str:
    issues = [
        "Incorrect information on your report•Information belongs to someone else",
        "Attempts to collect debt not owed•Debt was result of identity theft",
        "Getting a credit card•Card opened as result of identity theft or fraud",
    ]
    parameters: list[tuple[str, str]] = [
        ("field", "all"),
        ("format", "csv"),
        ("has_narrative", "true"),
        ("no_aggs", "true"),
        ("search_term", "identity theft"),
        ("sort", "created_date_desc"),
        ("date_received_min", date_min),
        ("date_received_max", date_max),
    ]
    parameters.extend(("issue", issue) for issue in issues)
    return (
        "https://www.consumerfinance.gov/data-research/"
        "consumer-complaints/search/api/v1/?"
        + urllib.parse.urlencode(parameters)
    )


DOWNLOADS: tuple[dict[str, str], ...] = (
    {
        "source": "OSHA_ITA",
        "url": "https://www.osha.gov/sites/default/files/ITA_Case_Detail_Data_2023_through_12-31-2023OIICS.zip",
        "relative_path": "osha_ita/osha_ita_2023.zip",
        "landing_page": "https://www.osha.gov/itadata",
        "license": "Public United States government data; source-specific terms apply",
    },
    {
        "source": "OSHA_ITA",
        "url": "https://www.osha.gov/sites/default/files/ITA_Case_Detail_Data_2024_through_12-31-2025.zip",
        "relative_path": "osha_ita/osha_ita_2024.zip",
        "landing_page": "https://www.osha.gov/itadata",
        "license": "Public United States government data; source-specific terms apply",
    },
    {
        "source": "OSHA_ITA",
        "url": "https://www.osha.gov/sites/default/largefiles/ITA_Case_Detail_Data_2025_through_3-15-2026.csv",
        "relative_path": "osha_ita/osha_ita_2025.csv",
        "landing_page": "https://www.osha.gov/itadata",
        "license": "Public United States government data; source-specific terms apply",
    },
    {
        "source": "USCG_NRC",
        "url": "https://nrc.uscg.mil/FOIAFiles/CYDECADE2010FirstHalf.zip",
        "relative_path": "uscg_nrc/CY2010_2014.zip",
        "landing_page": "https://nrc.uscg.mil/",
        "license": "Public FOIA release; no explicit dataset license stated",
    },
    {
        "source": "USCG_NRC",
        "url": "https://nrc.uscg.mil/FOIAFiles/CYDECADE2010SecondHalf.zip",
        "relative_path": "uscg_nrc/CY2015_2019.zip",
        "landing_page": "https://nrc.uscg.mil/",
        "license": "Public FOIA release; no explicit dataset license stated",
    },
    {
        "source": "USCG_NRC",
        "url": "https://nrc.uscg.mil/FOIAFiles/CYDECADE2020.zip",
        "relative_path": "uscg_nrc/CY2020_2024.zip",
        "landing_page": "https://nrc.uscg.mil/",
        "license": "Public FOIA release; no explicit dataset license stated",
    },
    {
        "source": "USCG_NRC",
        "url": "https://nrc.uscg.mil/FOIAFiles/DataDictionary.xlsx",
        "relative_path": "uscg_nrc/DataDictionary.xlsx",
        "landing_page": "https://nrc.uscg.mil/",
        "license": "Public FOIA release; no explicit dataset license stated",
    },
    *tuple(
        {
            "source": "CAL_OES",
            "url": url,
            "relative_path": f"caloes_spills/caloes_{year}.{extension}",
            "landing_page": (
                "https://www.caloes.ca.gov/office-of-the-director/operations/"
                "response-operations/fire-rescue/hazardous-materials/"
                "spill-release-reporting/"
            ),
            "license": "California public spill archive; attribution and source terms apply",
        }
        for year, extension, url in (
            (
                2016,
                "xls",
                "https://www.caloes.ca.gov/wp-content/uploads/Fire-Rescue/Documents/2016-HazMat-Spill-Reports.xls",
            ),
            (
                2017,
                "xls",
                "https://www.caloes.ca.gov/wp-content/uploads/Fire-Rescue/Documents/2017-HazMat-Spill-Reports.xls",
            ),
            (
                2018,
                "xls",
                "https://www.caloes.ca.gov/wp-content/uploads/Fire-Rescue/Documents/2018-HazMat-Spill-Reports.xls",
            ),
            (
                2019,
                "xlsx",
                "https://www.caloes.ca.gov/wp-content/uploads/Fire-Rescue/Documents/2019-Hazmat-Spill-Reports.xlsx",
            ),
            (
                2020,
                "xlsx",
                "https://www.caloes.ca.gov/wp-content/uploads/Fire-Rescue/Documents/2020-HazMat-Spill-Reports.xlsx",
            ),
            (
                2021,
                "xlsx",
                "https://www.caloes.ca.gov/wp-content/uploads/Fire-Rescue/Documents/2021-HazMat-Spill-Reports.xlsx",
            ),
            (
                2022,
                "xlsx",
                "https://www.caloes.ca.gov/wp-content/uploads/2023/04/2022-Spill-Reports.xls.xlsx",
            ),
            (
                2023,
                "xlsx",
                "https://www.caloes.ca.gov/wp-content/uploads/Fire-Rescue/Documents/2023-HazMat-Spill-Reports.xlsx",
            ),
            (
                2024,
                "xlsx",
                "https://www.caloes.ca.gov/wp-content/uploads/Fire-Rescue/Documents/2024-HazMat-Spill-Reports.xlsx",
            ),
        )
    ),
    {
        "source": "FAA_SDR",
        "url": "https://external.apic4e.faa.gov/sdrs/retrieve/SDR-2024.csv",
        "relative_path": "faa_sdr/SDR-2024.csv",
        "landing_page": "https://www.faa.gov/av-info/download_SDR",
        "license": "Public United States government data; source-specific terms apply",
    },
    {
        "source": "FAA_SDR",
        "url": "https://external.apic4e.faa.gov/sdrs/retrieve/SDR-2025.csv",
        "relative_path": "faa_sdr/SDR-2025.csv",
        "landing_page": "https://www.faa.gov/av-info/download_SDR",
        "license": "Public United States government data; source-specific terms apply",
    },
    *tuple(
        {
            "source": "FDA_RES",
            "url": (
                f"https://download.open.fda.gov/{product}/enforcement/"
                f"{product}-enforcement-0001-of-0001.json.zip"
            ),
            "relative_path": (
                f"fda_enforcement/{product}-enforcement-0001-of-0001.json.zip"
            ),
            "landing_page": "https://open.fda.gov/apis/downloads/",
            "license": "CC0 1.0 Universal unless otherwise noted",
        }
        for product in ("food", "drug", "device")
    ),
    {
        "source": "CFPB_CONSUMER_COMPLAINT",
        "url": _cfpb_url("2023-01-01", "2025-01-01"),
        "relative_path": "cfpb/cfpb_identity_theft_explicit_2023_2024.csv",
        "landing_page": "https://www.consumerfinance.gov/data-research/consumer-complaints/",
        "license": "CC0 1.0 Universal",
    },
    {
        "source": "CFPB_CONSUMER_COMPLAINT",
        "url": _cfpb_url("2025-01-01", "2025-07-01"),
        "relative_path": "cfpb/cfpb_identity_theft_explicit_2025_h1.csv",
        "landing_page": "https://www.consumerfinance.gov/data-research/consumer-complaints/",
        "license": "CC0 1.0 Universal",
    },
    {
        "source": "POLICE_UK",
        "url": "https://policeuk-data.s3.amazonaws.com/download/ef366b21e0bde33a29061699f5bdba915967fe36.zip",
        "relative_path": "police_uk/police_uk_metropolitan_2025-01_to_2025-03.zip",
        "landing_page": "https://data.police.uk/data/fetch/249cff9c-a6fc-4397-b3e5-f50acba54784/",
        "license": "Open Government Licence v3.0",
    },
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _retry_after_seconds(headers: object, fallback: float) -> float:
    """Return a bounded Retry-After delay, falling back to exponential backoff."""

    try:
        raw_value = headers.get("Retry-After")  # type: ignore[union-attr]
    except AttributeError:
        raw_value = None
    if raw_value is None:
        return min(fallback, MAXIMUM_RETRY_DELAY_SECONDS)

    value = str(raw_value).strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            seconds = (retry_at - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            seconds = fallback
    if not math.isfinite(seconds):
        seconds = fallback
    return min(max(seconds, 0.0), MAXIMUM_RETRY_DELAY_SECONDS)


def download_file(
    url: str,
    destination: Path,
    force: bool = False,
    *,
    maximum_attempts: int = DEFAULT_DOWNLOAD_ATTEMPTS,
    initial_backoff_seconds: float = DEFAULT_INITIAL_BACKOFF_SECONDS,
) -> None:
    """Download one source atomically, retrying transient network failures."""

    if maximum_attempts < 1:
        raise ValueError("maximum_attempts must be at least one")
    if (
        not math.isfinite(initial_backoff_seconds)
        or initial_backoff_seconds < 0
    ):
        raise ValueError("initial_backoff_seconds must be finite and non-negative")
    if destination.exists() and not force:
        print(f"Using existing download: {destination}")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    print(f"Downloading {url}")
    try:
        for attempt in range(1, maximum_attempts + 1):
            try:
                with urllib.request.urlopen(request, timeout=300) as response:
                    with temporary.open("wb") as output:
                        shutil.copyfileobj(
                            response,
                            output,
                            length=1024 * 1024,
                        )
                os.replace(temporary, destination)
                return
            except urllib.error.HTTPError as error:
                retryable = error.code in RETRYABLE_HTTP_STATUS_CODES
                if not retryable or attempt >= maximum_attempts:
                    raise
                fallback = initial_backoff_seconds * (2 ** (attempt - 1))
                delay = _retry_after_seconds(error.headers, fallback)
            except (
                urllib.error.URLError,
                TimeoutError,
                ConnectionError,
                http.client.HTTPException,
            ):
                if attempt >= maximum_attempts:
                    raise
                delay = min(
                    initial_backoff_seconds * (2 ** (attempt - 1)),
                    MAXIMUM_RETRY_DELAY_SECONDS,
                )

            print(
                f"Transient download failure; retrying in {delay:g}s "
                f"(attempt {attempt + 1}/{maximum_attempts}): {url}"
            )
            time.sleep(delay)
    finally:
        if temporary.exists():
            temporary.unlink()


def source_manifest(raw_directory: Path) -> dict[str, Any]:
    """Describe every present raw file without mutating or redownloading it."""

    files: list[dict[str, object]] = []
    missing: list[str] = []
    for item in DOWNLOADS:
        path = raw_directory / item["relative_path"]
        if not path.is_file():
            missing.append(str(path))
            continue
        files.append(
            {
                **item,
                "local_path": str(path),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return {
        "manifest_created_at_utc": datetime.now(timezone.utc).isoformat(),
        "files": files,
        "missing_files": missing,
    }


def write_source_manifest(raw_directory: Path) -> Path:
    manifest = source_manifest(raw_directory)
    manifest_path = raw_directory / "source_download_manifest.json"
    with manifest_path.open("w", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(f"Wrote source manifest: {manifest_path}")
    return manifest_path


def verify_source_manifest(
    raw_directory: Path,
    manifest_path: Path | None = None,
) -> dict[str, Any]:
    """Verify expected raw files against a stored manifest without mutation.

    The returned report has ``status == "PASS"`` only when every configured
    ``relative_path`` appears exactly once, resolves to the expected local path,
    exists as a regular file, and matches the stored byte size and SHA-256.
    Unexpected manifest entries are also reported so a stale or mixed-source
    manifest cannot be mistaken for the canonical source inventory.
    """

    stored_manifest_path = manifest_path or (
        raw_directory / "source_download_manifest.json"
    )
    report: dict[str, Any] = {
        "status": "FAIL",
        "manifest_path": str(stored_manifest_path),
        "expected_file_count": len(DOWNLOADS),
        "verified_file_count": 0,
        "missing_manifest_entries": [],
        "unexpected_manifest_entries": [],
        "duplicate_manifest_entries": [],
        "missing_local_files": [],
        "local_path_mismatches": [],
        "size_mismatches": [],
        "sha256_mismatches": [],
        "manifest_declared_missing_files": [],
        "manifest_schema_errors": [],
    }

    try:
        with stored_manifest_path.open(encoding="utf-8") as stream:
            stored = json.load(stream)
    except (OSError, json.JSONDecodeError) as error:
        report["manifest_schema_errors"].append(str(error))
        return report

    if not isinstance(stored, dict):
        report["manifest_schema_errors"].append(
            "Manifest root must be a JSON object"
        )
        return report
    raw_entries = stored.get("files")
    if not isinstance(raw_entries, list):
        report["manifest_schema_errors"].append(
            "Manifest field 'files' must be a list"
        )
        return report
    raw_declared_missing = stored.get("missing_files")
    if not isinstance(raw_declared_missing, list):
        report["manifest_schema_errors"].append(
            "Manifest field 'missing_files' must be a list"
        )
    else:
        report["manifest_declared_missing_files"] = [
            str(value) for value in raw_declared_missing
        ]

    entries_by_path: dict[str, dict[str, object]] = {}
    for index, raw_entry in enumerate(raw_entries):
        if not isinstance(raw_entry, dict):
            report["manifest_schema_errors"].append(
                f"Manifest files[{index}] must be an object"
            )
            continue
        relative_path = raw_entry.get("relative_path")
        if not isinstance(relative_path, str) or not relative_path.strip():
            report["manifest_schema_errors"].append(
                f"Manifest files[{index}].relative_path must be a non-empty string"
            )
            continue
        if relative_path in entries_by_path:
            report["duplicate_manifest_entries"].append(relative_path)
            continue
        entries_by_path[relative_path] = raw_entry

    expected_paths = {item["relative_path"] for item in DOWNLOADS}
    manifest_paths = set(entries_by_path)
    report["missing_manifest_entries"] = sorted(expected_paths - manifest_paths)
    report["unexpected_manifest_entries"] = sorted(manifest_paths - expected_paths)

    for relative_path in sorted(expected_paths):
        entry = entries_by_path.get(relative_path)
        local_path = raw_directory / relative_path
        if not local_path.is_file():
            report["missing_local_files"].append(str(local_path))
            continue
        if entry is None:
            continue

        declared_local_path = entry.get("local_path")
        if not isinstance(declared_local_path, str) or (
            Path(declared_local_path).resolve() != local_path.resolve()
        ):
            report["local_path_mismatches"].append(
                {
                    "relative_path": relative_path,
                    "expected": str(local_path),
                    "manifest": declared_local_path,
                }
            )

        actual_size = local_path.stat().st_size
        declared_size = entry.get("bytes")
        if (
            isinstance(declared_size, bool)
            or not isinstance(declared_size, int)
            or declared_size != actual_size
        ):
            report["size_mismatches"].append(
                {
                    "relative_path": relative_path,
                    "expected": declared_size,
                    "observed": actual_size,
                }
            )

        actual_sha256 = sha256_file(local_path)
        declared_sha256 = entry.get("sha256")
        if (
            not isinstance(declared_sha256, str)
            or declared_sha256.casefold() != actual_sha256
        ):
            report["sha256_mismatches"].append(
                {
                    "relative_path": relative_path,
                    "expected": declared_sha256,
                    "observed": actual_sha256,
                }
            )

        path_failed = any(
            mismatch["relative_path"] == relative_path
            for mismatch in report["local_path_mismatches"]
        )
        size_failed = any(
            mismatch["relative_path"] == relative_path
            for mismatch in report["size_mismatches"]
        )
        hash_failed = any(
            mismatch["relative_path"] == relative_path
            for mismatch in report["sha256_mismatches"]
        )
        if not (path_failed or size_failed or hash_failed):
            report["verified_file_count"] += 1

    failure_fields = (
        "missing_manifest_entries",
        "unexpected_manifest_entries",
        "duplicate_manifest_entries",
        "missing_local_files",
        "local_path_mismatches",
        "size_mismatches",
        "sha256_mismatches",
        "manifest_declared_missing_files",
        "manifest_schema_errors",
    )
    if not any(report[field] for field in failure_fields):
        report["status"] = "PASS"
    return report


def download_all(raw_directory: Path, force: bool = False) -> Path:
    raw_directory.mkdir(parents=True, exist_ok=True)
    for item in DOWNLOADS:
        download_file(
            item["url"],
            raw_directory / item["relative_path"],
            force=force,
        )
    return write_source_manifest(raw_directory)


__all__ = [
    "DOWNLOADS",
    "download_all",
    "download_file",
    "sha256_file",
    "source_manifest",
    "verify_source_manifest",
    "write_source_manifest",
]
