#!/usr/bin/env python3
"""Probe the actual required io_uring runtime using an isolated loopback process."""
import argparse
import socket
import subprocess
import time
from pathlib import Path


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, default=Path("reports/evidence"))
    args = parser.parse_args()
    binary = args.binary.resolve()
    if not binary.is_file():
        parser.error(f"binary does not exist: {binary}")
    args.evidence.mkdir(parents=True, exist_ok=True)
    port = free_port()
    log_path = args.evidence / "doctor.log"
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen([str(binary), "--bind", f"127.0.0.1:{port}"], stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    log.flush()
                    output = log_path.read_text(encoding="utf-8", errors="replace")
                    raise RuntimeError(f"native runtime probe failed; see {log_path}: {output}")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                        break
                except OSError:
                    time.sleep(0.05)
            else:
                raise TimeoutError("server did not start within 10 seconds")
            log.flush()
            output = log_path.read_text(encoding="utf-8", errors="replace")
            if "READY driver=io_uring" not in output:
                raise RuntimeError(f"listening process did not confirm io_uring; see {log_path}: {output}")
            print(f"PASS: native io_uring server accepted loopback connection on 127.0.0.1:{port}; log={log_path}")
        finally:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)


if __name__ == "__main__":
    main()
