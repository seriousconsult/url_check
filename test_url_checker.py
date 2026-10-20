import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

from url_checker import (
    URLCheckResult,
    check_bookmark_file,
    check_bookmark_folder,
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

    def test_bookmark_file_extracts_anchor_urls(self) -> None:
        html = '<DL><DT><A HREF="https://example.com/">Example</A></DL>'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bookmarks.html"
            path.write_text(html, encoding="utf-8")

            with patch("url_checker.check_urls", return_value=[]) as check:
                check_bookmark_file(path)

        check.assert_called_once_with(["https://example.com/"], max_workers=3, timeout=10.0)

    def test_bookmark_folder_combines_html_exports(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "B.html").write_text(
                '<A HREF="https://b.example/">B</A>', encoding="utf-8"
            )
            (folder / "a.htm").write_text(
                '<A HREF="https://a.example/">A</A>', encoding="utf-8"
            )
            (folder / "ignore.txt").write_text(
                '<A HREF="https://ignored.example/">Ignored</A>', encoding="utf-8"
            )

            with patch("url_checker.check_urls", return_value=[]) as check:
                check_bookmark_folder(folder)

        check.assert_called_once_with(
            ["https://a.example/", "https://b.example/"],
            max_workers=3,
            timeout=10.0,
        )

    def test_bookmark_folder_errors_if_no_exports(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FileNotFoundError):
                check_bookmark_folder(directory)

    def test_default_bookmark_folder_uses_project_bookmarks(self) -> None:
        with patch("url_checker.check_urls", return_value=[]) as check:
            check_bookmark_folder()

        urls = check.call_args.args[0]
        self.assertTrue(urls)
        self.assertEqual(check.call_args.kwargs, {"max_workers": 3, "timeout": 10.0})

    def test_worker_count_is_limited(self) -> None:
        with self.assertRaises(ValueError):
            check_urls([], max_workers=6)

    @patch("url_checker.check_url")
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

        def check_folder(**kwargs: object) -> list[URLCheckResult]:
            on_result = kwargs["on_result"]
            assert callable(on_result)
            for result in results:
                on_result(result)
            return results

        output = io.StringIO()
        with patch("url_checker.check_bookmark_folder", side_effect=check_folder):
            with redirect_stdout(output):
                main([])

        self.assertIn("ALIVE", output.getvalue())
        self.assertIn("DEAD", output.getvalue())
        self.assertIn("https://active.example", output.getvalue())
        self.assertIn("https://dead.example", output.getvalue())
        self.assertIn("Checked 2 URL(s): 1 active, 0 domain active, 1 dead.", output.getvalue())


if __name__ == "__main__":
    unittest.main()
