#!/usr/bin/env python3
import argparse, asyncio, sys, time
from urllib.parse import urlsplit


async def read_response(reader):
    length = 0
    while True:
        line = await reader.readline()
        if not line:
            return False
        if line == b"\r\n":
            break
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1])
    while length > 0:
        chunk = await reader.read(min(65536, length))
        if not chunk:
            return False
        length -= len(chunk)
    return True


async def run(url, concurrency, requests):
    u = urlsplit(url)
    host, port = u.hostname, u.port or 80
    path = (u.path or "/") + ("?" + u.query if u.query else "")
    request = (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
               "Connection: keep-alive\r\n\r\n").encode()
    sem, lock = asyncio.Semaphore(concurrency), asyncio.Lock()
    remaining = requests
    successes = errors = 0
    latencies = []

    async def worker():
        nonlocal remaining, successes, errors
        reader, writer = await asyncio.open_connection(host, port)
        try:
            while True:
                async with sem, lock:
                    if remaining <= 0:
                        return
                    remaining -= 1
                t0 = time.perf_counter_ns()
                writer.write(request)
                await writer.drain()
                if await read_response(reader):
                    successes += 1
                    latencies.append(time.perf_counter_ns() - t0)
                else:
                    errors += 1
        except ConnectionRefusedError:
            raise
        except (OSError, ConnectionError):
            errors += 1
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    await asyncio.gather(*(worker() for _ in range(min(concurrency, requests))))
    return successes, errors, latencies


def percentiles(values):
    if not values:
        return 0.0, 0.0, 0.0
    vals = sorted(values)
    n = len(vals) - 1
    return vals[int(.5 * n)] / 1e6, vals[int(.95 * n)] / 1e6, vals[int(.99 * n)] / 1e6


async def main():
    ap = argparse.ArgumentParser(description="Minimal HTTP/1.1 load generator")
    ap.add_argument("url", nargs="?", default="http://127.0.0.1:8000/health")
    ap.add_argument("-c", "--concurrency", type=int, default=50)
    ap.add_argument("-n", "--requests", type=int, default=1000)
    args = ap.parse_args()
    t0 = time.perf_counter()
    try:
        successes, errors, latencies = await run(args.url, args.concurrency, args.requests)
    except (OSError, ConnectionError) as e:
        print(f"connection failed: {e}", file=sys.stderr)
        sys.exit(1)
    elapsed = time.perf_counter() - t0
    p50, p95, p99 = percentiles(latencies)
    print(f"requests: {successes + errors}")
    print(f"successes: {successes}")
    print(f"errors: {errors}")
    print(f"requests/sec: {successes / elapsed:.1f}" if elapsed else "requests/sec: 0.0")
    print(f"latency ms: p50={p50:.2f} p95={p95:.2f} p99={p99:.2f}")


if __name__ == "__main__":
    asyncio.run(main())
