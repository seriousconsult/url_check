#!/usr/bin/env python3
"""Check URL and domain availability for Firefox bookmark exports and generate clean exports."""

from __future__ import annotations

import argparse
import math
import multiprocessing
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from html.parser import HTMLParser
import re
from pathlib import Path
from typing import Callable, Literal, Sequence
from urllib.parse import urlsplit, urlunsplit

import requests

from tor_diagnostic import is_using_tor


Availability = Literal["url_active", "domain_active", "dead", "unknown"]


@dataclass(frozen=True)
class URLCheckResult:
    url: str
    availability: Availability
    http_status: int | None = None
    final_url: str | None = None
    error: str | None = None


def _validate_timeout(timeout: float) -> None:
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and greater than zero")


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


def _check_url_worker(connection, url: str, timeout: float) -> None:
    """Send one result from an isolated, terminable network worker."""
    try:
        try:
            result = check_url(url, timeout)
        except Exception as error:
            result = URLCheckResult(
                url, "unknown", error=f"network worker failed ({type(error).__name__})"
            )
        connection.send(result)
    finally:
        connection.close()


def _bounded_check_url(url: str, timeout: float) -> URLCheckResult:
    # Spawn is safe from executor threads and works on Windows as well as Unix.
    context = multiprocessing.get_context("spawn")
    reader, writer = context.Pipe(duplex=False)
    process = context.Process(target=_check_url_worker, args=(writer, url, timeout))
    budget = 3 * timeout + 5  # Three request attempts plus startup overhead.
    try:
        try:
            process.start()
        except (OSError, RuntimeError) as error:
            return URLCheckResult(
                url, "unknown", error=f"could not start network worker ({type(error).__name__})"
            )
        writer.close()
        if reader.poll(budget):
            try:
                return reader.recv()
            except EOFError:
                return URLCheckResult(url, "unknown", error="network worker exited without a result")
        return URLCheckResult(
            url, "unknown", error=f"check exceeded {budget:g}s deadline; bookmark retained"
        )
    finally:
        reader.close()
        writer.close()
        if process.pid is not None:
            if process.is_alive():
                process.terminate()
            process.join(timeout=1)
            if process.is_alive():
                process.kill()
                process.join()
            process.close()


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
                futures[executor.submit(_bounded_check_url, url, timeout)] = index

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


class _MergeParser(HTMLParser):
    """Locate duplicate anchors while preserving the original export markup."""

    def __init__(self, html: str, seen: set[str]) -> None:
        super().__init__(convert_charrefs=True)
        self.html = html
        self.seen = seen
        self.offsets = [0]
        for match in re.finditer("\n", html):
            self.offsets.append(match.end())
        self.removals: list[tuple[int, int]] = []
        self.duplicate_start: int | None = None
        self.total = 0
        self.duplicates = 0

    def source_offset(self) -> int:
        line, column = self.getpos()
        return self.offsets[line - 1] + column

    def handle_starttag(self, tag, attrs) -> None:
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href")
        if not href:
            return
        self.total += 1
        # Query parameters do not distinguish bookmarks; keep the first full URL.
        parts = urlsplit(href)
        key = urlunsplit((parts.scheme, parts.netloc, parts.path, "", parts.fragment))
        if key in self.seen:
            self.duplicates += 1
            self.duplicate_start = self.source_offset()
        else:
            self.seen.add(key)

    def handle_endtag(self, tag) -> None:
        if tag.lower() == "a" and self.duplicate_start is not None:
            end = self.html.index(">", self.source_offset()) + 1
            start = self.duplicate_start
            prefix = re.search(r"<DT>\s*$", self.html[:start], re.IGNORECASE)
            if prefix:
                start = prefix.start()
            self.removals.append((start, end))
            self.duplicate_start = None


def merge_bookmark_files(input_files: Sequence[Path], output_file: Path) -> tuple[int, int]:
    """Merge exports in order, retaining the first URL regardless of query string."""
    if any(path.resolve() == output_file.resolve() for path in input_files):
        raise ValueError("Merged output must not overwrite an input file")
    seen: set[str] = set()
    sections = []
    total = duplicates = 0
    for path in input_files:
        html = path.read_text(encoding="utf-8-sig")
        parser = _MergeParser(html, seen)
        parser.feed(html)
        parser.close()
        for start, end in reversed(parser.removals):
            html = html[:start] + html[end:]
        # Netscape exports wrap all folders and bookmarks in one outer DL.
        opening = re.search(r"<DL\b[^>]*>", html, re.IGNORECASE)
        closings = list(re.finditer(r"</DL\s*>", html, re.IGNORECASE))
        if opening and closings:
            sections.append(html[opening.end():closings[-1].start()])
        elif parser.total:
            raise ValueError(f"Bookmark export has no enclosing DL: {path}")
        total += parser.total
        duplicates += parser.duplicates
    output_file.write_text(
        '<!DOCTYPE NETSCAPE-Bookmark-file-1>\n'
        '<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">\n'
        '<TITLE>Bookmarks</TITLE>\n<H1>Bookmarks</H1>\n<DL><p>\n'
        + "\n".join(sections) + '\n</DL><p>\n',
        encoding="utf-8",
    )
    return total - duplicates, duplicates


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


def _select_merge_files(files: Sequence[Path]) -> list[Path]:
    print("Bookmark files available to merge:", flush=True)
    for number, path in enumerate(files, start=1):
        print(f"  {number}. {path.name}", flush=True)
    if len(files) < 2:
        print("At least two files are required to merge.")
        return []
    while True:
        try:
            answer = input(
                "Which files should be merged? Enter numbers (e.g. 1,3), 'all', or 'cancel': "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return []
        if answer in {"", "cancel"}:
            return []
        if answer == "all":
            return list(files)
        try:
            numbers = {int(value) for value in re.split(r"[,\s]+", answer)}
        except ValueError:
            print("Enter file numbers separated by commas or spaces, or 'all'.")
            continue
        if len(numbers) < 2 or not numbers <= set(range(1, len(files) + 1)):
            print("Select at least two different file numbers from the list.")
            continue
        return [path for number, path in enumerate(files, start=1) if number in numbers]


def main(argv: Sequence[str] | None = None) -> int:
    """CLI to process single files or scan the entire ./bookmarks folder."""
    argument_parser = argparse.ArgumentParser(
        description="Filter dead domains from Firefox bookmark HTML exports."
    )
    argument_parser.add_argument(
        "--merge",
        action="store_true",
        help="choose folder exports to merge, ignoring query strings for duplicates",
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
        help="custom cleaned output path (single file or merged folder)",
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

    _validate_timeout(args.timeout)
    # Determine execution mode (Directory scan vs Single File)
    target_path = args.target if args.target is not None else Path("bookmarks")

    if not target_path.exists():
        print(f"Error: Target path does not exist: {target_path}")
        return 1

    files_to_process: list[tuple[Path, Path]] = []

    if target_path.is_dir():
        html_files = sorted(
            [p for p in target_path.iterdir()
             if p.is_file() and p.suffix.lower() in {".html", ".htm"}
             and not p.stem.lower().endswith("_clean")
             and p.name.lower() != "merged_bookmarks.html"]
        )
        if not html_files:
            print(f"Error: No .html/.htm bookmark files found inside directory: {target_path}")
            return 1

        merge = args.merge
        if len(html_files) > 1 and not merge:
            try:
                answer = input(f"Merge {len(html_files)} bookmark files and remove duplicates first? [y/N]: ")
            except (EOFError, KeyboardInterrupt):
                print("Cancelled. No files written.")
                return 0
            merge = answer.strip().lower() in {"y", "yes"}
        if args.output and not merge:
            argument_parser.error("--output requires a single file or a merged folder")
        if merge:
            selected_files = _select_merge_files(html_files)
            if not selected_files:
                print("Cancelled. No files written.")
                return 0
            merged_file = target_path / "merged_bookmarks.html"
            out_file = args.output or target_path / "merged_bookmarks_clean.html"
            if out_file.resolve() in {p.resolve() for p in [*html_files, merged_file]}:
                argument_parser.error("cleaned output must not overwrite an input or the merged export")
            try:
                unique, duplicates = merge_bookmark_files(selected_files, merged_file)
            except (OSError, ValueError) as error:
                print(f"Error merging bookmarks: {error}")
                return 1
            print(f"Merged {len(selected_files)} files: {unique} bookmarks, {duplicates} duplicates removed.", flush=True)
            print(f"Merged export saved to: {merged_file.resolve()}", flush=True)
            files_to_process.append((merged_file, out_file))
        else:
            for file in html_files:
                out_file = file.parent / f"{file.stem}_clean{file.suffix}"
                files_to_process.append((file, out_file))
    else:
        if args.merge:
            argument_parser.error("--merge requires a folder of bookmark exports")
        out_file = args.output if args.output else target_path.parent / f"{target_path.stem}_clean{target_path.suffix}"
        files_to_process.append((target_path, out_file))

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
        print("Cancelled. No bookmarks were checked or cleaned exports written.")
        return 0

    def print_result(result: URLCheckResult) -> None:
        status = str(result.http_status) if result.http_status is not None else "-"
        details = f" ({result.error})" if result.error else ""
        label = {
            "url_active": "ALIVE",
            "domain_active": "URL DEAD / DOMAIN ALIVE",
            "dead": "DEAD (REMOVING)",
            "unknown": "TIMED OUT / RETAINED",
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
            f"  - {counts['unknown']} inconclusive checks retained\n"
            f"  - Cleaned file saved to: {out_file.resolve()}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
