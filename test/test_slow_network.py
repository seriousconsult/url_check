import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import requests

from slow_network import SlowNetworkProxy
from url_checker import _bounded_check_url, main


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/redirect':
            self.send_response(302)
            self.send_header('Location', '/ok')
            self.end_headers()
            return
        body = b'x' * 4096
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class SlowNetworkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def test_latency_bandwidth_and_redirects(self):
        with SlowNetworkProxy(latency=0.1, jitter=0, bandwidth=16) as proxy:
            started = time.monotonic()
            result = requests.get(self.url + '/redirect',
                                  proxies={'http': proxy.url}, timeout=3)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(len(result.content), 4096)
            self.assertGreaterEqual(time.monotonic() - started, 0.4)

    def test_connect_tunnel(self):
        with SlowNetworkProxy(latency=0, jitter=0, bandwidth=1024) as proxy:
            port = int(proxy.url.rsplit(':', 1)[1])
            with socket.create_connection(('127.0.0.1', port), timeout=3) as client:
                client.sendall(f'CONNECT 127.0.0.1:{self.server.server_port} HTTP/1.1\r\n\r\n'.encode())
                self.assertIn(b'200 Connection Established', client.recv(4096))
                client.sendall(b'GET /ok HTTP/1.1\r\nHost: localhost\r\n\r\n')
                self.assertIn(b'200 OK', client.recv(4096))

    def test_latency_causes_real_request_timeout(self):
        with SlowNetworkProxy(latency=0.3, jitter=0, bandwidth=32) as proxy:
            with self.assertRaises(requests.Timeout):
                requests.get(self.url, proxies={'http': proxy.url}, timeout=0.05)

    def test_spawned_worker_uses_proxy(self):
        with SlowNetworkProxy(latency=0, jitter=0, bandwidth=1024) as proxy:
            self.assertEqual(_bounded_check_url(self.url + '/ok', 2, proxy.url).availability,
                             'url_active')

    def test_cli_passes_proxy_to_diagnostic_and_bookmark_checks(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'bookmarks.html'
            source.write_text('<DL></DL>')
            with patch('url_checker.is_using_tor', return_value=False) as diagnostic, \
                 patch('builtins.input', return_value='yes'), \
                 patch('url_checker.process_single_file', return_value=([], 0)) as process:
                self.assertEqual(main([str(source), '--simulate-slow', '--slow-latency', '0']), 0)
            proxy_url = process.call_args.kwargs['proxy_url']
            self.assertEqual(diagnostic.call_args.kwargs['session'].proxies['https'], proxy_url)
            with self.assertRaises(OSError):
                socket.create_connection(('127.0.0.1', int(proxy_url.rsplit(':', 1)[1])), timeout=0.1)

    def test_invalid_settings(self):
        for settings in ({'latency': -1}, {'jitter': float('nan')}, {'bandwidth': 0}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                SlowNetworkProxy(**settings)
