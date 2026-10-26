import threading
import multiprocessing
import time
import unittest
from unittest.mock import patch

from url_checker import URLCheckResult, check_urls, _bounded_check_url


class ProgressTests(unittest.TestCase):
    def test_slow_request_does_not_block_next_submission(self):
        third_started = threading.Event()

        def check(url, timeout):
            if url == "first":
                self.assertTrue(third_started.wait(2), "next URL was blocked by the slow batch")
            elif url == "third":
                third_started.set()
            return URLCheckResult(url, "url_active")

        with patch("url_checker._bounded_check_url", side_effect=check):
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

        with patch("url_checker._bounded_check_url", return_value=URLCheckResult("one", "url_active")), \
             patch("url_checker.wait", side_effect=wait_once), \
             patch("builtins.print"):
            progress = []
            check_urls(["one"], on_wait=lambda done, total: progress.append((done, total)))
        self.assertEqual(progress, [(0, 1)])


def hanging_worker(connection, url, timeout):
    if url == "hang":
        time.sleep(60)
    else:
        connection.send(URLCheckResult(url, "url_active"))
    connection.close()


def exiting_worker(connection, url, timeout):
    connection.close()


class HardDeadlineTests(unittest.TestCase):
    def test_hang_is_terminated_while_other_urls_keep_completing(self):
        reported = []
        before = {p.pid for p in multiprocessing.active_children()}
        started = time.monotonic()
        with patch("url_checker._check_url_worker", hanging_worker):
            results = check_urls(
                ["hang", "second", "third", "fourth"], max_workers=2,
                timeout=0.01, on_result=lambda result: reported.append(result.url),
            )
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual(reported[-1], "hang")
        self.assertEqual(results[0].availability, "unknown")
        self.assertIn("deadline", results[0].error)
        self.assertEqual([r.url for r in results], ["hang", "second", "third", "fourth"])
        self.assertEqual({p.pid for p in multiprocessing.active_children()}, before)

    def test_worker_exit_is_inconclusive(self):
        with patch("url_checker._check_url_worker", exiting_worker):
            result = _bounded_check_url("exit", 0.01)
        self.assertEqual(result.availability, "unknown")

    def test_real_spawned_check(self):
        self.assertEqual(_bounded_check_url("invalid-url", 0.1).availability, "dead")
