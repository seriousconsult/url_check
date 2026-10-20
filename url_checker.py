"""Check URL and domain availability for Firefox bookmark exports."""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Literal, Sequence
from urllib.parse import urlsplit

import requests


Availability = Literal["url_active", "domain_active", "dead"]


@dataclass(frozen=True)
class URLCheckResult:
    url: str
    availability: Availability
    http_status: int | None = None
    final_url: str | None = None
    error: str | None = None


def _validate_timeout(timeout: float) -> None:
    if timeout <= 0:
        raise ValueError("timeout must be greater than zero")


def _origin_url(url: str, scheme: str) -> str:
    parts = urlsplit(url)
    hostname = parts.hostname
    if hostname is None:
        raise ValueError("URL has no hostname")

    host = f"[{hostname}]" if ":" in hostname else hostname
    port = parts.port
    netloc = f"{host}:{port}" if port is not None else host
    return f"{scheme}://{netloc}/"


def check_url(url: str, timeout: float = 10.0) -> URLCheckResult:
    """Classify one URL as active, domain-only active, or dead.

    Any HTTP response below 400 marks the URL active. An HTTP error response
    marks the domain active. If the URL request fails, the domain root is
    checked over the original scheme and then the alternate HTTP(S) scheme.
    Request failures are returned in ``error`` rather than treated as success.
    """
    _validate_timeout(timeout)
    if not isinstance(url, str):
        raise TypeError("url must be a string")

    try:
        parts = urlsplit(url)
        if parts.scheme.lower() not in {"http", "https"} or parts.hostname is None:
            return URLCheckResult(url=url, availability="dead", error="invalid HTTP(S) URL")
        # Accessing port also validates malformed port numbers.
        parts.port
    except ValueError:
        return URLCheckResult(url=url, availability="dead", error="invalid HTTP(S) URL")

    try:
        with requests.get(url, timeout=timeout, stream=True) as response:
            availability: Availability = (
                "url_active" if response.status_code < 400 else "domain_active"
            )
            return URLCheckResult(
                url=url,
                availability=availability,
                http_status=response.status_code,
                final_url=response.url,
            )
    except requests.RequestException as url_error:
        url_error_type = type(url_error).__name__

    original_scheme = parts.scheme.lower()
    alternate_scheme = "http" if original_scheme == "https" else "https"
    for scheme in (original_scheme, alternate_scheme):
        try:
            with requests.get(
                _origin_url(url, scheme),
                timeout=timeout,
                stream=True,
            ) as response:
                return URLCheckResult(
                    url=url,
                    availability="domain_active",
                    http_status=response.status_code,
                    final_url=response.url,
                    error=f"URL request failed ({url_error_type})",
                )
        except (requests.RequestException, ValueError):
            continue

    return URLCheckResult(
        url=url,
        availability="dead",
        error=f"URL and domain requests failed ({url_error_type})",
    )


class _BookmarkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.urls: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag.lower() == "a":
            for name, value in attrs:
                if name.lower() == "href" and value:
                    self.urls.append(value)


def check_urls(
    urls: list[str],
    max_workers: int = 3,
    timeout: float = 10.0,
    on_result: Callable[[URLCheckResult], None] | None = None,
) -> list[URLCheckResult]:
    """Check URLs concurrently, preserving result order and reporting completions."""
    if not 2 <= max_workers <= 5:
        raise ValueError("max_workers must be between 2 and 5")
    _validate_timeout(timeout)

    completed: dict[int, URLCheckResult] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for start in range(0, len(urls), max_workers):
            futures = {
                executor.submit(check_url, url, timeout): index
                for index, url in enumerate(urls[start : start + max_workers], start)
            }
            for future in as_completed(futures):
                result = future.result()
                completed[futures[future]] = result
                if on_result is not None:
                    on_result(result)

    return [completed[index] for index in range(len(urls))]


def check_bookmark_file(
    file_path: str | Path,
    max_workers: int = 3,
    timeout: float = 10.0,
    on_result: Callable[[URLCheckResult], None] | None = None,
) -> list[URLCheckResult]:
    """Parse Firefox bookmark HTML and check each bookmarked URL."""
    parser = _parse_bookmark_file(Path(file_path))
    if on_result is None:
        return check_urls(parser.urls, max_workers=max_workers, timeout=timeout)
    return check_urls(
        parser.urls,
        max_workers=max_workers,
        timeout=timeout,
        on_result=on_result,
    )


def check_bookmark_folder(
    folder_path: str | Path | None = None,
    max_workers: int = 3,
    timeout: float = 10.0,
    on_result: Callable[[URLCheckResult], None] | None = None,
) -> list[URLCheckResult]:
    """Check exports in a folder, defaulting to this project's bookmarks folder."""
    folder = (
        Path(folder_path)
        if folder_path is not None
        else Path(__file__).resolve().parent / "bookmarks"
    )
    if not folder.is_dir():
        raise NotADirectoryError(f"Bookmark export folder does not exist: {folder}")

    bookmark_files = sorted(
        (
            path
            for path in folder.iterdir()
            if path.is_file() and path.suffix.lower() in {".html", ".htm"}
        ),
        key=lambda path: path.name.casefold(),
    )
    if not bookmark_files:
        raise FileNotFoundError(f"No HTML bookmark exports found in: {folder}")

    urls: list[str] = []
    for bookmark_file in bookmark_files:
        urls.extend(_parse_bookmark_file(bookmark_file).urls)

    if on_result is None:
        return check_urls(urls, max_workers=max_workers, timeout=timeout)
    return check_urls(
        urls,
        max_workers=max_workers,
        timeout=timeout,
        on_result=on_result,
    )


def _parse_bookmark_file(file_path: Path) -> _BookmarkParser:
    html = file_path.read_text(encoding="utf-8-sig")
    parser = _BookmarkParser()
    parser.feed(html)
    parser.close()
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the checker from the command line and print results as they finish."""
    argument_parser = argparse.ArgumentParser(
        description="Check URLs in Firefox bookmark HTML exports."
    )
    argument_parser.add_argument(
        "folder",
        nargs="?",
        help="folder containing .html/.htm exports (default: ./bookmarks)",
    )
    argument_parser.add_argument(
        "--workers",
        type=int,
        default=3,
        choices=range(2, 6),
        metavar="{2,3,4,5}",
        help="number of simultaneous checks (default: 3)",
    )
    argument_parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="timeout in seconds per request (default: 10)",
    )
    args = argument_parser.parse_args(argv)

    print("Checking bookmarks...", flush=True)

    def print_result(result: URLCheckResult) -> None:
        status = str(result.http_status) if result.http_status is not None else "-"
        details = f" ({result.error})" if result.error else ""
        label = {
            "url_active": "ALIVE",
            "domain_active": "URL DEAD / DOMAIN ALIVE",
            "dead": "DEAD",
        }[result.availability]
        print(f"{label:23} {status:>3}  {result.url}{details}", flush=True)

    results = check_bookmark_folder(
        folder_path=args.folder,
        max_workers=args.workers,
        timeout=args.timeout,
        on_result=print_result,
    )
    counts = Counter(result.availability for result in results)
    print(
        f"\nChecked {len(results)} URL(s): "
        f"{counts['url_active']} active, "
        f"{counts['domain_active']} domain active, "
        f"{counts['dead']} dead."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
