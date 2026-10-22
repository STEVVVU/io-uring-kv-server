# io_uring key-value server

A bounded Rust TCP key-value service running on Monoio's native Linux io_uring driver. It implements atomic batched writes, snapshot reads, admission control, a concurrent-history checker and an offered-load benchmark.

This repository preserves the complete [Monoio](https://github.com/bytedance/monoio) runtime and its history at `8675e9406ea7943c441d969e38c3f5fc198ef6d1`. The service and verification tooling are additions in [`kv-server/`](kv-server/). The [original README](README.upstream.md) and MIT/Apache-2.0 notices are retained.

## Run

Use Rust 1.99.1 and Python 3 on Linux with io_uring permitted by the kernel and process policy:

```sh
cargo build --locked --release -p monoio-kv-server
target/release/monoio-kv-server --bind 127.0.0.1:9000 --batch-mode adaptive
```

The server accepts only numeric loopback addresses. It constructs `RuntimeBuilder<IoUringDriver>` directly and reports the actual driver on startup. It exits explicitly on unsupported hosts or denied io_uring setup. Storage is in memory and is lost when the process stops.

## Design

All storage operations execute on one local runtime thread. A request parses and validates its entire payload before admission, then changes or snapshots the map without an await. MPUT stages all changes before mutation; MGET observes one state. Separate connection tasks yield at network I/O.

The binary protocol supports GET, PUT, MGET and MPUT. Frames are capped at 1 MiB, keys at 1 KiB, values at 16 KiB and batches at 32 items. Stored key/value bytes are limited to 64 MiB and 100,000 keys; concurrent connections are capped at 64. See the [protocol and controller details](kv-server/README.md).

Fixed mode uses configured connection and request-size limits. Adaptive mode adjusts connection admission after windows of accepted/rejected connections and bounds accepted batch size using active connection occupancy. This is a small admission policy inspired by [SEDA](https://www.sosp.org/2001/papers/welsh.pdf); it does not dynamically combine requests or reproduce SEDA's full staged architecture.

## Verify

```sh
cargo fmt --all -- --check
cargo test --locked -p monoio-kv-server
cargo clippy --locked --no-deps -p monoio-kv-server --all-targets -- -D warnings
python3 kv-server/scripts/doctor.py --binary target/release/monoio-kv-server --evidence reports/evidence
python3 kv-server/scripts/verify.py --binary target/release/monoio-kv-server --evidence reports/evidence
python3 kv-server/scripts/benchmark.py --binary target/release/monoio-kv-server --evidence reports/evidence
```

The real-product driver checks exact GET/PUT values, atomic batched histories, fragmented network I/O, malformed requests, storage limits and connection-capacity recovery. It records owned process IDs, cleanup results and a durable failing verdict when an assertion fails. The [verification skill](.agents/skills/verify/SKILL.md) maps the lifecycle and negative control.

The benchmark interleaves fixed/adaptive runs using the same seeded operation stream, bounded client queue and Poisson arrivals. It retains raw latency samples and reports offered, attempted, completed, rejected, timed-out and failed counts separately. Wire latency, client queue delay and offer-to-completion latency are separate measurements.

Portable Rust checks have passed on Windows, including a Linux-target compile check. Native Linux behavior and load measurements are pending the first hosted run. No university grading SLO, multicore scaling, persistence or general latency advantage is claimed. This is an independently scoped service informed by the [Brown CSCI 1675 systems topic](https://cs.brown.edu/courses/csci1675/fall2025/hw/project-2.html), without course starter code or grader compatibility.

The sole active GitHub workflow runs read-only native Linux checks and uploads evidence. Inherited runtime automation is preserved in [`.github/upstream-workflows/`](.github/upstream-workflows/).
