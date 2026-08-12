"""End-to-end tests for scripts/bench.py against a local threaded HTTP server."""

import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

BENCH = Path(__file__).resolve().parent.parent / "scripts" / "bench.py"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        body = b'{"status":"ok"}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture()
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


def run_bench(url, concurrency, requests):
    return subprocess.run(
        [sys.executable, str(BENCH), url, "-c", str(concurrency), "-n", str(requests)],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_bench_reports_stats(server):
    url = f"http://127.0.0.1:{server.server_port}/health"
    result = run_bench(url, concurrency=3, requests=9)
    assert result.returncode == 0
    assert "requests: 9" in result.stdout
    assert "successes: 9" in result.stdout
    assert "errors: 0" in result.stdout
    assert "requests/sec:" in result.stdout
    assert "latency ms" in result.stdout


def test_bench_connection_refused():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    result = run_bench(f"http://127.0.0.1:{port}/health", concurrency=2, requests=4)
    assert result.returncode == 1
    assert "connection failed" in result.stderr
