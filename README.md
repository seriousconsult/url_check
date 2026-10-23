# URL Checker

Check URLs saved in a Firefox bookmarks HTML export. The checker uses a
limited number of concurrent requests and classifies each URL as:

- `url_active`: the URL returned an HTTP response with a status below 400.
- `domain_active`: the URL returned an HTTP error status, or the URL request
  failed but a request to the domain root succeeded.
- `dead`: the URL and domain root could not be reached, or the bookmark is not
  a valid HTTP(S) URL.

These results describe what the checker could reach during its run; `dead`
does not prove that a site is permanently offline. Network conditions,
firewalls, rate limits, and bot protection can affect results.

## Requirements

- Python 3.10 or newer
- Dependencies listed in `requirements.txt`

Install the dependency:

```powershell
python -m pip install -r requirements.txt
```

## Export Firefox bookmarks

In Firefox, open the Library with **Ctrl+Shift+O**, select **Import and
Backup**, then **Export Bookmarks to HTML**. Save the exported HTML file in a
location you can pass to the checker.

## Check bookmarks in a folder

Run the checker directly from the project directory to check the `bookmarks`
folder and display each result as it completes:

```powershell
python url_checker.py
```

Before checking bookmarks, the command contacts the Tor Project's check
endpoint and reports whether its requests use Tor (active, inactive, or
unknown if the diagnostic fails). Type `yes` at the prompt to continue.
Any other response, end of input, or Ctrl+C cancels before checking bookmarks
or writing cleaned exports. This checks the current connection; it does not
start Tor or configure a proxy.

You can specify another folder and adjust concurrency and timeout:

```powershell
python url_checker.py "C:\path\to\bookmark-exports" --workers 4 --timeout 15
```

The command prints one status line per URL, followed by a summary. It defaults
to 3 simultaneous requests; choose between 2 and 5 workers.

For use from another Python script:

```python
from url_checker import check_bookmark_folder

results = check_bookmark_folder(max_workers=3, timeout=10.0)

for result in results:
    print(
        result.availability,
        result.http_status,
        result.url,
        result.error or "",
    )
```

By default, `check_bookmark_folder` reads the `bookmarks` folder next to
`url_checker.py`. You can pass a different folder path if needed. It checks
`.html` and `.htm` files directly in that folder (it does not scan subfolders),
combines their links in alphabetical file order, and returns a list of
`URLCheckResult` objects in bookmark order. It raises `FileNotFoundError` if
there are no HTML exports in the folder. Each result has:

- `url`: the bookmarked URL.
- `availability`: `url_active`, `domain_active`, or `dead`.
- `http_status`: the last relevant HTTP status, if a server responded.
- `final_url`: the response URL after redirects, if available.
- `error`: a brief request failure description, if applicable.

To check one exported file, use `check_bookmark_file("bookmarks.html")`. To
check individual URLs or an existing list, use `check_url(url)` or
`check_urls(urls)`. `check_urls` also preserves input order.

## Concurrency and timeouts

`max_workers` defaults to 3 and can be set from 2 to 5. Higher concurrency
may increase load on your network and the sites being checked. The timeout
defaults to 10 seconds per request; failed URL checks may also try the domain
root over HTTP and HTTPS.

Workers start the next URL as soon as a check finishes. While no results
arrive, the command prints a progress update every five seconds. The timeout
applies to each request's network waits, not the entire run; a failed URL
can require three requests. Cleaned exports are written after each input
file finishes.

## Tor diagnostic

`tor_diagnostic.py` provides `is_using_tor()`, which checks whether the
request's observed public IP is identified as a Tor exit node:

```python
from tor_diagnostic import is_using_tor

print(is_using_tor())
```

To check using an explicitly configured `requests.Session`, pass that session
to `is_using_tor(session)`. The diagnostic contacts the Tor Project's public
check endpoint. SOCKS proxy support requires installing Requests with its
optional SOCKS dependency (for example, `python -m pip install "requests[socks]"`).

## Run tests

```powershell
python -m unittest -v test_url_checker
```
