import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import incident_pipeline.download as download


class DownloadRetryTests(unittest.TestCase):
    def test_retryable_http_error_honours_retry_after_and_succeeds(self) -> None:
        error = urllib.error.HTTPError(
            "https://example.test/source.csv",
            429,
            "Too Many Requests",
            {"Retry-After": "0"},
            None,
        )
        response = io.BytesIO(b"authentic source bytes")

        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "source.csv"
            with (
                patch(
                    "incident_pipeline.download.urllib.request.urlopen",
                    side_effect=[error, response],
                ) as urlopen,
                patch("incident_pipeline.download.time.sleep") as sleep,
            ):
                download.download_file(
                    "https://example.test/source.csv",
                    destination,
                    maximum_attempts=3,
                    initial_backoff_seconds=0.25,
                )

            self.assertEqual(destination.read_bytes(), b"authentic source bytes")
            self.assertEqual(urlopen.call_count, 2)
            sleep.assert_called_once_with(0.0)
            self.assertFalse(destination.with_suffix(".csv.part").exists())

    def test_non_retryable_http_error_is_raised_without_sleeping(self) -> None:
        error = urllib.error.HTTPError(
            "https://example.test/missing.csv",
            404,
            "Not Found",
            {},
            None,
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "missing.csv"
            with (
                patch(
                    "incident_pipeline.download.urllib.request.urlopen",
                    side_effect=error,
                ) as urlopen,
                patch("incident_pipeline.download.time.sleep") as sleep,
            ):
                with self.assertRaises(urllib.error.HTTPError):
                    download.download_file(
                        "https://example.test/missing.csv",
                        destination,
                        maximum_attempts=5,
                    )

            self.assertEqual(urlopen.call_count, 1)
            sleep.assert_not_called()
            self.assertFalse(destination.exists())
            self.assertFalse(destination.with_suffix(".csv.part").exists())

    def test_network_error_uses_exponential_backoff(self) -> None:
        response = io.BytesIO(b"downloaded")
        failures = [
            urllib.error.URLError("temporary DNS failure"),
            TimeoutError("temporary timeout"),
            response,
        ]

        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "source.zip"
            with (
                patch(
                    "incident_pipeline.download.urllib.request.urlopen",
                    side_effect=failures,
                ),
                patch("incident_pipeline.download.time.sleep") as sleep,
            ):
                download.download_file(
                    "https://example.test/source.zip",
                    destination,
                    maximum_attempts=3,
                    initial_backoff_seconds=0.5,
                )

            self.assertEqual(destination.read_bytes(), b"downloaded")
            self.assertEqual(
                [call.args[0] for call in sleep.call_args_list],
                [0.5, 1.0],
            )


class SourceManifestVerificationTests(unittest.TestCase):
    DOWNLOAD_FIXTURES = (
        {
            "source": "SOURCE_A",
            "url": "https://example.test/a.csv",
            "relative_path": "source_a/a.csv",
            "landing_page": "https://example.test/a",
            "license": "Test license A",
        },
        {
            "source": "SOURCE_B",
            "url": "https://example.test/b.zip",
            "relative_path": "source_b/b.zip",
            "landing_page": "https://example.test/b",
            "license": "Test license B",
        },
    )

    def _write_sources(self, raw_directory: Path) -> None:
        for index, item in enumerate(self.DOWNLOAD_FIXTURES):
            path = raw_directory / item["relative_path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"fixture-{index}".encode("ascii"))

    def test_valid_manifest_passes_without_modifying_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            raw_directory = Path(temporary_directory)
            self._write_sources(raw_directory)
            with patch.object(download, "DOWNLOADS", self.DOWNLOAD_FIXTURES):
                manifest_path = download.write_source_manifest(raw_directory)
                manifest_before = manifest_path.read_bytes()
                report = download.verify_source_manifest(raw_directory)

            self.assertEqual(report["status"], "PASS")
            self.assertEqual(report["expected_file_count"], 2)
            self.assertEqual(report["verified_file_count"], 2)
            self.assertEqual(manifest_path.read_bytes(), manifest_before)

    def test_missing_file_and_checksum_drift_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            raw_directory = Path(temporary_directory)
            self._write_sources(raw_directory)
            with patch.object(download, "DOWNLOADS", self.DOWNLOAD_FIXTURES):
                download.write_source_manifest(raw_directory)
                (raw_directory / "source_a" / "a.csv").write_bytes(b"tampered-0")
                (raw_directory / "source_b" / "b.zip").unlink()
                report = download.verify_source_manifest(raw_directory)

            self.assertEqual(report["status"], "FAIL")
            self.assertEqual(
                report["missing_local_files"],
                [str(raw_directory / "source_b" / "b.zip")],
            )
            self.assertEqual(
                [item["relative_path"] for item in report["sha256_mismatches"]],
                ["source_a/a.csv"],
            )
            self.assertEqual(
                [item["relative_path"] for item in report["size_mismatches"]],
                ["source_a/a.csv"],
            )
            self.assertEqual(report["verified_file_count"], 0)

    def test_stale_manifest_schema_fails_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            raw_directory = Path(temporary_directory)
            manifest_path = raw_directory / "source_download_manifest.json"
            manifest_path.write_text(
                json.dumps({"sources": {"legacy": {"sha256": "abc"}}}),
                encoding="utf-8",
            )
            with patch.object(download, "DOWNLOADS", self.DOWNLOAD_FIXTURES):
                report = download.verify_source_manifest(raw_directory)

            self.assertEqual(report["status"], "FAIL")
            self.assertIn("must be a list", report["manifest_schema_errors"][0])


if __name__ == "__main__":
    unittest.main()
