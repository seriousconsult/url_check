#!/usr/bin/env python3
"""Local HTTP/HTTPS proxy with artificial latency, jitter, and bandwidth limits."""

from __future__ import annotations

import argparse
import math
import random
import select
import socket
import socketserver
import threading
import time
from urllib.parse import urlsplit


class SlowNetworkProxy:
    """A loopback-only testing proxy; limits apply per connection and direction."""

    def __init__(self, latency: float = 1.5, jitter: float = 0.5,
                 bandwidth: float = 32.0, port: int = 0):
        for name, value in (("latency", latency), ("jitter", jitter), ("bandwidth", bandwidth)):
            if not math.isfinite(value) or value < 0 or (name == "bandwidth" and value == 0):
                raise ValueError(f"{name} must be finite and {'positive' if name == 'bandwidth' else 'nonnegative'}")
        self.latency = latency
        self.jitter = jitter
        self.bandwidth = bandwidth * 1024
        self._stop = threading.Event()
        proxy = self

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                try:
                    proxy._handle(self.request)
                except (OSError, ValueError):
                    pass  # Clients may time out or close while a delayed write is pending.

        self._server = Server(("127.0.0.1", port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *args):
        self._stop.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()

    def _handle(self, client):
        client.settimeout(10)
        header = bytearray()
        while b"\r\n\r\n" not in header:
            chunk = client.recv(4096)
            if not chunk:
                return
            header.extend(chunk)
            if len(header) > 65536:
                return
        first, rest = bytes(header).split(b"\r\n", 1)
        method, target, version = first.decode("ascii").split(" ", 2)
        if method == "CONNECT":
            address = urlsplit("//" + target)
            port = address.port or 443
            initial = bytes(header).split(b"\r\n\r\n", 1)[1]
        else:
            address = urlsplit(target)
            if address.scheme != "http":
                return
            port = address.port or 80
            path = address.path or "/"
            if address.query:
                path += "?" + address.query
            initial = f"{method} {path} {version}\r\n".encode("ascii") + rest
        if not address.hostname:
            return
        delay = max(0, self.latency + random.uniform(-self.jitter, self.jitter))
        if self._stop.wait(delay):
            return
        try:
            upstream = socket.create_connection((address.hostname, port), timeout=10)
        except OSError:
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            return
        with upstream:
            if method == "CONNECT":
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            # Schedule each direction separately so uploads do not stall downloads.
            pending = {client: bytearray(), upstream: bytearray(initial)}
            next_write = {client: time.monotonic(), upstream: time.monotonic()}
            reading = {client, upstream}
            while not self._stop.is_set():
                now = time.monotonic()
                writable = [s for s in pending if pending[s] and now >= next_write[s]]
                readable = [s for s in reading if len(pending[upstream if s is client else client]) < 65536]
                ready_read, ready_write, _ = select.select(readable, writable, [], 0.02)
                for source in ready_read:
                    data = source.recv(4096)
                    destination = upstream if source is client else client
                    if data:
                        pending[destination].extend(data)
                    else:
                        reading.discard(source)
                for destination in ready_write:
                    # Small chunks keep low bandwidth settings responsive.
                    size = max(1, min(4096, int(self.bandwidth * 0.02)))
                    sent = destination.send(pending[destination][:size])
                    del pending[destination][:sent]
                    next_write[destination] = time.monotonic() + sent / self.bandwidth
                for source, destination in ((client, upstream), (upstream, client)):
                    if source not in reading and not pending[destination]:
                        return


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8888)
    parser.add_argument("--latency", type=float, default=1.5, help="connection delay in seconds")
    parser.add_argument("--jitter", type=float, default=0.5, help="random +/- delay in seconds")
    parser.add_argument("--bandwidth", type=float, default=32, help="KiB/s per connection per direction")
    args = parser.parse_args(argv)
    try:
        proxy = SlowNetworkProxy(args.latency, args.jitter, args.bandwidth, args.port)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    with proxy:
        print(f"Slow testing proxy: {proxy.url} (no Tor routing or anonymity)", flush=True)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
