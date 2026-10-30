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

## Simulate a slow connection

Run through an automatically managed local testing proxy:

```powershell
python url_checker.py bookmarks.html --simulate-slow
python url_checker.py bookmarks.html --simulate-slow --slow-latency 3 --slow-jitter 1 --slow-bandwidth 16 --timeout 30
```

Defaults add 1.5 seconds of connection delay with +/- 0.5 seconds of jitter,
then limit traffic to 32 KiB/s per connection in each direction. Limits apply
independently to each worker connection; they are not a shared bandwidth cap.
HTTP and HTTPS (CONNECT tunnels), redirects, domain probes, and the Tor status
request all use the proxy. HTTPS remains encrypted end to end.

To run the simulator separately, leave this command running in another terminal:

```powershell
python slow_network.py --port 8888 --latency 1.5 --jitter 0.5 --bandwidth 32
python url_checker.py bookmarks.html --proxy http://127.0.0.1:8888
```

Stop the standalone simulator with Ctrl+C. The automatic simulator closes when
URL Checker exits. It listens only on localhost and requires no extra packages.
This approximates slow Tor-like timing; it does not provide Tor routing,
anonymity, or exact Tor behavior. Proxy mode connects directly from this machine
and overrides environment proxy settings. Omit both network options to retain
normal connection behavior.

Latency applies when opening each proxy connection; bandwidth limits apply to
all forwarded bytes, including TLS handshakes. Because URL Checker streams only
response headers, large page bodies usually do not affect its checks. Low
bandwidth or high latency can trigger the existing request timeouts and overall
worker deadline; increase `--timeout` for slow successful checks. Existing
classification rules still apply: failed URL and domain probes can mark a
bookmark dead, so use disposable bookmark copies when testing timeouts.
