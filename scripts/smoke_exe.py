#!/usr/bin/env python3
"""Smoke-test a built ``clipforge-server`` executable.

Copies the exe to a temp dir (so the test never touches the repo or the
release staging dir — the exe must be self-contained), starts it, polls
``/health`` and ``/`` until they answer, prints what they return, then
stops the server. Exits 0 only when the exe boots and answers /health.

Usage:

    python scripts/smoke_exe.py dist/clipforge-server[.exe] [--port PORT]
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path


def _get(url: str, timeout: float = 3.0) -> tuple[int, bytes]:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.status, resp.read()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _expected_version() -> str:
    text = (Path(__file__).resolve().parents[1] / "src" / "clipforge" / "__init__.py").read_text(
        encoding="utf-8"
    )
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    return match.group(1) if match else "?"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("exe", type=Path, help="path to the built clipforge-server exe")
    parser.add_argument(
        "--port", type=int, default=0, help="port to test on (default: pick a free one)"
    )
    parser.add_argument(
        "--timeout", type=int, default=60, help="seconds to wait for /health (default 60)"
    )
    args = parser.parse_args(argv)

    if not args.exe.is_file():
        raise SystemExit(f"exe not found: {args.exe}")
    port = args.port or _free_port()
    expected_version = _expected_version()

    tmp = Path(tempfile.mkdtemp(prefix="clipforge-smoke-"))
    exe_copy = tmp / args.exe.name
    shutil.copy2(args.exe, exe_copy)
    exe_copy.chmod(0o755)
    print(f"Copied exe to {exe_copy} (isolated temp dir)", flush=True)

    proc = subprocess.Popen(
        [str(exe_copy), "--host", "127.0.0.1", "--port", str(port)],
        cwd=tmp,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base = f"http://127.0.0.1:{port}"
    print(f"Testing on {base} (expecting version {expected_version})", flush=True)
    deadline = time.time() + args.timeout
    health: dict | None = None
    try:
        while time.time() < deadline:
            if proc.poll() is not None:
                out = proc.stdout.read() if proc.stdout else ""
                print(f"FAIL: exe exited early with code {proc.returncode}", flush=True)
                print(out[-4000:], flush=True)
                return 1
            try:
                status, body = _get(f"{base}/health")
                if status == 200:
                    health = json.loads(body)
                    break
            except Exception:
                time.sleep(1)
        if health is None:
            print(f"FAIL: /health did not answer within {args.timeout}s", flush=True)
            return 1
        if health.get("version") != expected_version:
            print(
                f"FAIL: /health version {health.get('version')!r} != expected "
                f"{expected_version!r} — is another server on this port?",
                flush=True,
            )
            return 1
        print(f"PASS: GET /health -> {health}", flush=True)
        status, body = _get(f"{base}/")
        print(
            f"PASS: GET / -> HTTP {status}, {len(body)} bytes (API-only status page)"
            if b"ClipForge API" in body
            else f"PASS: GET / -> HTTP {status}, {len(body)} bytes",
            flush=True,
        )
        status, body = _get(f"{base}/jobs")
        print(f"PASS: GET /jobs -> HTTP {status}: {body.decode()[:200]}", flush=True)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)
    print("Smoke test PASSED", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
