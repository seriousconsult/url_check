import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import requests

from url_checker import main


class StartupTests(unittest.TestCase):
    def test_status_and_explicit_confirmation(self):
        for status, label in ((True, "ACTIVE"), (False, "INACTIVE"),
                              (requests.ConnectionError("offline"), "UNKNOWN")):
            for answer in ("yes", "", "y", "no", EOFError(), KeyboardInterrupt()):
                with self.subTest(status=label, answer=answer), tempfile.TemporaryDirectory() as directory:
                    source = Path(directory) / "bookmarks.html"
                    source.write_text("<DL></DL>", encoding="utf-8")
                    output = io.StringIO()

                    def confirm(prompt):
                        self.assertIn(f"Tor status: {label}", output.getvalue())
                        process.assert_not_called()
                        if isinstance(answer, BaseException):
                            raise answer
                        return answer

                    with patch("url_checker.is_using_tor", side_effect=status if isinstance(status, Exception) else None,
                               return_value=status) as diagnostic, \
                         patch("builtins.input", side_effect=confirm), \
                         patch("url_checker.process_single_file", return_value=([], 0)) as process, \
                         redirect_stdout(output):
                        self.assertEqual(main([str(source), "--timeout", "2"]), 0)

                    diagnostic.assert_called_once_with(timeout=2.0)
                    self.assertEqual(process.call_count, 1 if answer == "yes" else 0)
                    self.assertFalse(source.with_name("bookmarks_clean.html").exists())


if __name__ == "__main__":
    unittest.main()
