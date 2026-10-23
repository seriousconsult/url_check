import threading
import unittest
from unittest.mock import patch

from url_checker import URLCheckResult, check_urls


class ProgressTests(unittest.TestCase):
    def test_slow_request_does_not_block_next_submission(self):
        third_started = threading.Event()

        def check(url, timeout):
            if url == "first":
                self.assertTrue(third_started.wait(2), "next URL was blocked by the slow batch")
            elif url == "third":
                third_started.set()
            return URLCheckResult(url, "url_active")

        with patch("url_checker.check_url", side_effect=check):
            results = check_urls(["first", "second", "third"], max_workers=2)
        self.assertEqual([result.url for result in results], ["first", "second", "third"])

    def test_wait_reports_progress(self):
        from concurrent.futures import wait as real_wait

        calls = []

        def wait_once(futures, **kwargs):
            if not calls:
                calls.append(True)
                return set(), set(futures)
            return real_wait(futures, **kwargs)

        with patch("url_checker.check_url", return_value=URLCheckResult("one", "url_active")), \
             patch("url_checker.wait", side_effect=wait_once), \
             patch("builtins.print"):
            progress = []
            check_urls(["one"], on_wait=lambda done, total: progress.append((done, total)))
        self.assertEqual(progress, [(0, 1)])
