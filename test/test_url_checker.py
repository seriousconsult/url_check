#!/usr/bin/env python3

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

from url_checker import (
    URLCheckResult,
    process_single_file,
    check_url,
    check_urls,
    main,
)


def response(status_code: int, url: str = "https://example.com/path") -> MagicMock:
    result = MagicMock()
    result.__enter__.return_value = result
    result.status_code = status_code
    result.url = url
    return result


class URLCheckerTests(unittest.TestCase):
    @patch("url_checker.requests.get")
    def test_successful_url_is_active(self, get: MagicMock) -> None:
        get.return_value = response(200)

        result = check_url("https://example.com/path")

        self.assertEqual(result.availability, "url_active")
        self.assertEqual(result.http_status, 200)

    @patch("url_checker.requests.get")
    def test_http_error_marks_domain_active(self, get: MagicMock) -> None:
        get.return_value = response(404)

        result = check_url("https://example.com/missing")

        self.assertEqual(result.availability, "domain_active")
        self.assertEqual(result.http_status, 404)

    @patch("url_checker.requests.get")
    def test_failed_url_probes_domain_root(self, get: MagicMock) -> None:
        get.side_effect = [
            requests.ConnectionError("unavailable path"),
            response(200, "https://example.com/"),
        ]

        result = check_url("https://example.com/path")

        self.assertEqual(result.availability, "domain_active")
        self.assertEqual(result.error, "URL request failed (ConnectionError)")
        self.assertEqual(get.call_count, 2)

    @patch("url_checker.requests.get")
    def test_failed_url_and_root_are_dead(self, get: MagicMock) -> None:
        get.side_effect = requests.ConnectionError("unavailable")

        result = check_url("https://example.com/path")

        self.assertEqual(result.availability, "dead")

    def test_cleaned_export_retains_inconclusive_bookmarks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "bookmarks.html"
            output = Path(directory) / "clean.html"
            source.write_text(
                '<DT><A HREF="https://slow.example/">Slow</A>\n'
                '<DT><A HREF="https://dead.example/">Dead</A>\n',
                encoding="utf-8",
            )
            results = [
                URLCheckResult("https://slow.example/", "unknown"),
                URLCheckResult("https://dead.example/", "dead"),
            ]
            with patch("url_checker.check_urls", return_value=results):
                _, removed = process_single_file(source, output)
            self.assertEqual(removed, 1)
            self.assertIn("https://slow.example/", output.read_text())
            self.assertNotIn("https://dead.example/", output.read_text())

    def test_worker_count_is_limited(self) -> None:
        with self.assertRaises(ValueError):
            check_urls([], max_workers=6)

    @patch("url_checker._bounded_check_url")
    def test_check_urls_reports_results_and_preserves_order(
        self,
        check: MagicMock,
    ) -> None:
        check.side_effect = lambda url, timeout: URLCheckResult(
            url=url,
            availability="url_active",
        )
        reported: list[URLCheckResult] = []

        results = check_urls(
            ["https://a.example", "https://b.example"],
            on_result=reported.append,
        )

        self.assertEqual([item.url for item in results], [
            "https://a.example",
            "https://b.example",
        ])
        self.assertCountEqual(reported, results)

    def test_cli_prints_each_url_and_summary(self) -> None:
        results = [
            URLCheckResult("https://active.example", "url_active", 200),
            URLCheckResult("https://dead.example", "dead", error="ConnectionError"),
        ]

        def process(**kwargs):
            for result in results:
                kwargs["on_result"](result)
            return results, 1

        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "bookmarks.html"
            source.write_text("<DL></DL>")
            with patch("url_checker.process_single_file", side_effect=process), \
                 patch("url_checker.is_using_tor", return_value=False), \
                 patch("builtins.input", return_value="yes"), redirect_stdout(output):
                main([str(source)])

        self.assertIn("ALIVE", output.getvalue())
        self.assertIn("DEAD", output.getvalue())
        self.assertIn("https://active.example", output.getvalue())
        self.assertIn("https://dead.example", output.getvalue())
        self.assertIn("1 dead domains removed", output.getvalue())


if __name__ == "__main__":
    unittest.main()
