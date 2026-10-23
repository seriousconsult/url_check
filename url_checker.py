#!/usr/bin/env python3
"""Check URL and domain availability for Firefox bookmark exports and generate clean exports."""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from html.parser import HTMLParser
import re
from pathlib import Path
from typing import Callable, Literal, Sequence
from urllib.parse import urlsplit

import requests

from tor_diagnostic import is_using_tor


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
    """Classify one URL as active, domain-only active, or dead."""
    _validate_timeout(timeout)
    if not isinstance(url, str):
        raise TypeError("url must be a string")

    try:
        parts = urlsplit(url)
        if parts.scheme.lower() not in {"http", "https"} or parts.hostname is None:
            return URLCheckResult(url=url, availability="dead", error="invalid HTTP(S) URL")
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
    on_wait: Callable[[int, int], None] | None = None,
) -> list[URLCheckResult]:
    """Check URLs concurrently, preserving result order."""
    if not 2 <= max_workers <= 5:
        raise ValueError("max_workers must be between 2 and 5")
    _validate_timeout(timeout)

    completed: dict[int, URLCheckResult] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        remaining = iter(enumerate(urls))
        futures = {}

        def submit_next() -> None:
            item = next(remaining, None)
            if item is not None:
                index, url = item
                futures[executor.submit(check_url, url, timeout)] = index

        for _ in range(max_workers):
            submit_next()
        while futures:
            done, _ = wait(futures, timeout=5.0, return_when=FIRST_COMPLETED)
            if not done and on_wait is not None:
                on_wait(len(completed), len(urls))
            for future in done:
                result = future.result()
                completed[futures.pop(future)] = result
                if on_result is not None:
                    on_result(result)
                submit_next()

    return [completed[index] for index in range(len(urls))]


def _parse_bookmark_file(file_path: Path) -> _BookmarkParser:
    html = file_path.read_text(encoding="utf-8-sig")
    parser = _BookmarkParser()
    parser.feed(html)
    parser.close()
    return parser


def process_single_file(
    input_file: Path,
    output_file: Path,
    max_workers: int = 3,
    timeout: float = 10.0,
    on_result: Callable[[URLCheckResult], None] | None = None,
    on_wait: Callable[[int, int], None] | None = None,
) -> tuple[list[URLCheckResult], int]:
    """Check URLs for a single HTML file and output a cleaned version."""
    parser = _parse_bookmark_file(input_file)
    results = check_urls(
        parser.urls,
        max_workers=max_workers,
        timeout=timeout,
        on_result=on_result,
        on_wait=on_wait,
    )

    dead_urls = {res.url for res in results if res.availability == "dead"}
    raw_html = input_file.read_text(encoding="utf-8-sig")

    pattern = re.compile(
        r"^[ \t]*<DT><A\b[^>]*\bHREF=[\"'](?P<url>[^\"']+)[\"'][^>]*>.*?</A>.*?\n?",
        flags=re.IGNORECASE | re.MULTILINE,
    )

    removed_count = 0

    def replace_dead(match: re.Match) -> str:
        nonlocal removed_count
        href = match.group("url")
        if href in dead_urls:
            removed_count += 1
            return ""
        return match.group(0)

    cleaned_html = pattern.sub(replace_dead, raw_html)
    output_file.write_text(cleaned_html, encoding="utf-8")
    return results, removed_count


def main(argv: Sequence[str] | None = None) -> int:
    """CLI to process single files or scan the entire ./bookmarks folder."""
    argument_parser = argparse.ArgumentParser(
        description="Filter dead domains from Firefox bookmark HTML exports."
    )
    argument_parser.add_argument(
        "target",
        nargs="?",
        type=Path,
        help="path to a specific .html file OR a folder (default: ./bookmarks)",
    )
    argument_parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="custom output path (only valid when processing a single file)",
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

    # Determine execution mode (Directory scan vs Single File)
    target_path = args.target if args.target is not None else Path("bookmarks")

    if not target_path.exists():
        print(f"Error: Target path does not exist: {target_path}")
        return 1

    files_to_process: list[tuple[Path, Path]] = []

    if target_path.is_dir():
        html_files = sorted(
            [p for p in target_path.iterdir() if p.is_file() and p.suffix.lower() in {".html", ".htm"}]
        )
        if not html_files:
            print(f"Error: No .html/.htm bookmark files found inside directory: {target_path}")
            return 1

        for file in html_files:
            out_file = file.parent / f"{file.stem}_clean{file.suffix}"
            files_to_process.append((file, out_file))
    else:
        out_file = args.output if args.output else target_path.parent / f"{target_path.stem}_clean{target_path.suffix}"
        files_to_process.append((target_path, out_file))

    _validate_timeout(args.timeout)
    print("Checking Tor connection...", flush=True)
    try:
        using_tor = is_using_tor(timeout=args.timeout)
    except (requests.RequestException, ValueError) as error:
        print(f"Tor status: UNKNOWN (diagnostic failed: {error})", flush=True)
    else:
        print(
            "Tor status: ACTIVE (requests use Tor)"
            if using_tor
            else "Tor status: INACTIVE (requests do not use Tor)",
            flush=True,
        )

    try:
        confirmation = input("Type 'yes' to continue checking bookmarks: ")
    except (EOFError, KeyboardInterrupt):
        confirmation = ""
    if confirmation.strip().lower() != "yes":
        print("Cancelled. No bookmarks were checked or output files written.")
        return 0

    def print_result(result: URLCheckResult) -> None:
        status = str(result.http_status) if result.http_status is not None else "-"
        details = f" ({result.error})" if result.error else ""
        label = {
            "url_active": "ALIVE",
            "domain_active": "URL DEAD / DOMAIN ALIVE",
            "dead": "DEAD (REMOVING)",
        }[result.availability]
        print(f"{label:23} {status:>3}  {result.url}{details}", flush=True)

    for in_file, out_file in files_to_process:
        print(f"\nProcessing '{in_file}'...", flush=True)
        results, removed_count = process_single_file(
            input_file=in_file,
            output_file=out_file,
            max_workers=args.workers,
            timeout=args.timeout,
            on_result=print_result,
            on_wait=lambda completed, total: print(
                f"Still checking: {completed}/{total} URLs completed; waiting for network responses...",
                flush=True,
            ),
        )

        counts = Counter(res.availability for res in results)
        print(
            f"Finished {in_file.name}:\n"
            f"  - {counts['url_active']} active URLs\n"
            f"  - {counts['domain_active']} domain active URLs\n"
            f"  - {counts['dead']} dead domains removed\n"
            f"  - Cleaned file saved to: {out_file.resolve()}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
