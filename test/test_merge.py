import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from url_checker import _parse_bookmark_file, main, merge_bookmark_files


class MergeTests(unittest.TestCase):
    def test_merge_ignores_queries_and_preserves_first_bookmark_and_folders(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second, output = [root / name for name in ("a.html", "b.html", "merged.html")]
            first.write_text('<DL><p><DT><H3>Folder</H3><DL><p>\n'
                             '<DT><A HREF="https://example.com/page?a=1&amp;b=2" ADD_DATE="123">First</A>\n'
                             '</DL><p></DL><p>', encoding="utf-8")
            second.write_text('<DL><p>\n<DT><A HREF="https://example.com/page?other=3">Duplicate</A>\n'
                              '<DT><A HREF="https://example.com/other">Other</A>\n'
                              '<DT><A HREF="https://example.com/other">Repeated</A>\n</DL><p>', encoding="utf-8")
            self.assertEqual(merge_bookmark_files([first, second], output), (2, 2))
            self.assertEqual(_parse_bookmark_file(output).urls,
                             ["https://example.com/page?a=1&b=2", "https://example.com/other"])
            html = output.read_text(encoding="utf-8")
            self.assertIn('<H3>Folder</H3>', html)
            self.assertIn('ADD_DATE="123">First</A>', html)
            self.assertNotIn("Duplicate", html)
            with self.assertRaises(ValueError):
                merge_bookmark_files([first, second], first)

    def test_startup_merges_before_tor_then_checks_only_merged_file(self):
        for use_flag in (False, True):
            with self.subTest(use_flag=use_flag), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for name in ("a.html", "b.html", "a_clean.html", "merged_bookmarks.html"):
                    (root / name).write_text('<DL><p><DT><A HREF="https://example.com/">Example</A>\n</DL><p>')

                def diagnostic(**kwargs):
                    self.assertEqual(_parse_bookmark_file(root / "merged_bookmarks.html").urls,
                                     ["https://example.com/"])
                    return False

                with patch("builtins.input", side_effect=["all", "yes"] if use_flag else ["yes", "all", "yes"]), \
                     patch("url_checker.is_using_tor", side_effect=diagnostic), \
                     patch("url_checker.process_single_file", return_value=([], 0)) as process, \
                     redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(main([str(root)] + (["--merge"] if use_flag else [])), 0)
                self.assertIn("Merged 2 files: 1 bookmarks, 1 duplicates removed", output.getvalue())
                self.assertEqual(process.call_count, 1)
                self.assertEqual(process.call_args.kwargs["input_file"], root / "merged_bookmarks.html")

    def test_selection_checks_only_selected_files_and_reprompts_invalid_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("a", "b", "c"):
                (root / f"{name}.html").write_text(
                    f'<DL><p><DT><A HREF="https://{name}.example/">{name}</A>\n</DL><p>'
                )
            with patch("builtins.input", side_effect=["1", "1,9", "bad", "3,1,3", "yes"]), \
                 patch("url_checker.is_using_tor", return_value=False), \
                 patch("url_checker.process_single_file", return_value=([], 0)) as process, \
                 redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main([str(root), "--merge"]), 0)
            self.assertEqual(_parse_bookmark_file(root / "merged_bookmarks.html").urls,
                             ["https://a.example/", "https://c.example/"])
            self.assertIn("2. b.html", output.getvalue())
            process.assert_called_once()

    def test_cancelling_selection_does_not_write_files_or_check_connection(self):
        for answer in ("cancel", "", EOFError(), KeyboardInterrupt()):
            with self.subTest(answer=answer), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for name in ("a.html", "b.html"):
                    (root / name).write_text("<DL></DL>")
                with patch("builtins.input", side_effect=[answer]), \
                     patch("url_checker.is_using_tor") as diagnostic, \
                     redirect_stdout(io.StringIO()):
                    self.assertEqual(main([str(root), "--merge"]), 0)
                diagnostic.assert_not_called()
                self.assertFalse((root / "merged_bookmarks.html").exists())


if __name__ == "__main__":
    unittest.main()
