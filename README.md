# URL Checker

Merge Firefox bookmark exports, remove duplicates, and check which sites are reachable.
Requires Python 3.10+.

## Setup and run

```powershell
python -m pip install -r requirements.txt
python url_checker.py
```

Export bookmarks from Firefox with **Ctrl+Shift+O → Import and Backup → Export
Bookmarks to HTML**, and put the files in the `bookmarks` folder.

1. Choose whether to merge, then select files by number (e.g. `1,3`) or `all`.
   Enter `cancel` to stop. Duplicates ignore query strings;
   the first bookmark keeps its original URL and title. Folders are preserved.
2. Review the Tor connection status, then type `yes` to start checking.
3. Watch live results. Unreachable domains are removed; reachable domains and
   inconclusive checks are kept. Original exports remain unchanged.

Merging creates `merged_bookmarks.html`; checking saves
`merged_bookmarks_clean.html`. Separate checks save `<name>_clean.html`.
Generated files are excluded from folder scans. The Tor check reports your
current connection; it does not configure Tor.

## Options

```powershell
python url_checker.py --merge
python url_checker.py "C:\path\to\exports" --workers 4 --timeout 15
python url_checker.py bookmarks.html --output cleaned.html
```

`--merge` opens the file selection directly. Workers: 2–5 (default 3).
Timeout: seconds per request (default 10).

## Tests

```powershell
python -m unittest discover -s test -v
```
