#!/usr/bin/env python3
"""Performance check: single-message import latency with 100k stored messages.

Usage:
    python scripts/bench.py [--base-url http://127.0.0.1:8000] [--total 100000]

Seeds ``--total`` messages into a fresh mailbox (batched imports of 500),
then measures latency of single-message imports (raw size <= 8 KiB) and of
GET /threads.  Exits non-zero if any single-message import exceeds 2 seconds.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
import uuid

import httpx

BODY = ("Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 110)[:6144]


def make_eml(mid: str, parent: str | None = None, body: str = "seed") -> str:
    lines = [
        "From: bench@example.com",
        "To: bench@example.com",
        "Subject: bench",
        f"Message-ID: <{mid}>",
    ]
    if parent:
        lines.append(f"In-Reply-To: <{parent}>")
    return "\r\n".join(lines) + "\r\n\r\n" + body + "\r\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--total", type=int, default=100_000)
    ap.add_argument("--batch", type=int, default=500)
    ap.add_argument("--samples", type=int, default=30)
    ap.add_argument("--limit", type=float, default=2.0)
    args = ap.parse_args()

    mailbox = f"bench-{uuid.uuid4().hex[:12]}"
    client = httpx.Client(base_url=args.base_url, timeout=120.0)

    # ---- seed -------------------------------------------------------------
    print(f"seeding {args.total} messages into mailbox {mailbox!r} ...")
    t0 = time.perf_counter()
    sent = 0
    thread_no = 0
    anchor_ids: list[str] = []
    while sent < args.total:
        emls = []
        for _ in range(args.batch // 50):  # threads of 50 messages each
            thread_no += 1
            root = f"seed-{thread_no}-0@bench"
            anchor_ids.append(root)
            emls.append(make_eml(root))
            for i in range(1, 50):
                emls.append(make_eml(f"seed-{thread_no}-{i}@bench", f"seed-{thread_no}-{i - 1}@bench"))
        r = client.post("/imports", json={"mailbox": mailbox, "messages": emls})
        r.raise_for_status()
        sent += len(emls)
        print(f"\r{sent}/{args.total}", end="", flush=True)
    print(f"\nseeded in {time.perf_counter() - t0:.1f}s")

    # ---- measure single-message imports ------------------------------------
    raw = make_eml("placeholder@bench", body=BODY)  # <= 8 KiB check below
    assert len(raw.encode()) <= 8192, f"raw message too big: {len(raw.encode())} bytes"

    lat = []
    for i in range(args.samples):
        parent = anchor_ids[i % len(anchor_ids)]
        eml = make_eml(f"probe-{i}@bench", parent, BODY)
        size = len(eml.encode())
        assert size <= 8192, size
        t0 = time.perf_counter()
        r = client.post("/imports", json={"mailbox": mailbox, "messages": [eml]})
        dt = time.perf_counter() - t0
        r.raise_for_status()
        lat.append(dt)

    # ---- measure GET /threads ----------------------------------------------
    t0 = time.perf_counter()
    r = client.get("/threads", params={"mailbox": mailbox})
    get_dt = time.perf_counter() - t0
    r.raise_for_status()
    threads = len(r.json()["threads"])

    print(f"\nmailbox now holds {args.total + args.samples} messages, {threads} threads")
    print(f"single-message import latency over {args.samples} samples:")
    print(f"  min    {min(lat) * 1000:8.1f} ms")
    print(f"  median {statistics.median(lat) * 1000:8.1f} ms")
    print(f"  p95    {sorted(lat)[int(len(lat) * 0.95) - 1] * 1000:8.1f} ms")
    print(f"  max    {max(lat) * 1000:8.1f} ms")
    print(f"GET /threads (full forest): {get_dt * 1000:.1f} ms")

    worst = max(lat)
    if worst > args.limit:
        print(f"FAIL: slowest import {worst:.2f}s exceeds {args.limit}s budget")
        return 1
    print(f"OK: every import finished within {args.limit}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
