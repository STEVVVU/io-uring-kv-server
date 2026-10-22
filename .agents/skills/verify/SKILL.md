---
name: verify
description: Drive the Monoio KV service over its real Linux TCP/io_uring interface, check native runtime readiness, capture durable protocol/concurrency/overload evidence, and compare bounded fixed/adaptive load runs.
---

# Verify Monoio KV service

Proved on native hosted Ubuntu in run 39713710063 at product commit 7f1cf5dc57a61fd21e330f133b5178752c713939: Launch, Doctor, Drive, retained Evidence and Cleanup passed. The injected post-launch assertion failed as intended while preserving passed=false and a reaped owned process. Curated proof is in docs/evidence/linux-39713710063; raw samples remain in the workflow artifact. Windows itself cannot run io_uring.

## Launch

Build from the repository root on Linux: `cargo build --locked --release -p monoio-kv-server`. Launch an isolated loopback instance with `target/release/monoio-kv-server --bind 127.0.0.1:9000 --batch-mode adaptive --max-connections 64`. The CLI accepts literal loopback socket addresses only. Readiness is the flushed `READY driver=io_uring address=...` line and a successful TCP connect. Runtime construction explicitly requires native io_uring. If it fails with EPERM or an unsupported-kernel error, record that as a blocked native run; never substitute the legacy driver. Stop only the PID launched by this run.

## Doctor

Run `python kv-server/scripts/doctor.py --binary target/release/monoio-kv-server --evidence reports/evidence`. It launches a short-lived instance on an OS-assigned loopback port, requires the `READY driver=io_uring` output, confirms a TCP connection, then terminates and reaps only that process. It preserves `doctor.log`, including startup errors, as evidence. A failure means the instance is not ready to drive.

## Drive

Run `python kv-server/scripts/verify.py --binary target/release/monoio-kv-server --evidence reports/evidence`. This launches the real TCP service in fixed and adaptive modes, exercises fragmented frames and requests, validates GET/PUT/MGET/MPUT and exact value round trips, drives concurrent same-key multi-key batches, checks the JSON-safe invocation/response history with an independent sequential-spec backtracking checker, verifies the checker rejects a deliberately fractured snapshot, checks externally reachable bind rejection, sends malformed/truncated/oversized traffic, saturates a non-reading peer while checking another peer still progresses, tests admission capacity/recovery, then stops and reaps every process it started. To prove failure-evidence and cleanup handling intentionally, add `--inject-failure-after-launch`; this should fail the command and leave `verification.json` with `passed: false`, the launched PID, and its cleanup result.

Run `python kv-server/scripts/benchmark.py --binary target/release/monoio-kv-server --evidence reports/evidence` for four interleaved bounded Poisson offered-load runs (fixed, adaptive, adaptive, fixed). Defaults are 3 seconds per run, 48 persistent clients, and 1200 offered operations per second. The harness drains all arrivals due at each scheduler tick, caps queued work at twice client concurrency, uses one shared deterministic seed, and reports offered work separately from tasks that actually start, completed, rejected, timed-out, and failed operations. Wire latency excludes client queue delay; the report includes both separately and offer-to-completion latency. Worker sockets are explicitly closed after the pool drains. Adjust only the documented bounded benchmark flags when a longer/shorter run is needed.

## Evidence

The longer native campaign is maintained in `kv-server/scripts/campaign.py` with
the stdlib Go client `load.go`. Its frozen workloads, correctness checks,
histogram precision, resource accounting and reproduction are in
`kv-server/BENCHMARK.md`. Run the client `-self-test` before a campaign; use the
manual `metrics.yml` workflow. Keep traced runs separate from measured trials.

The verification JSON, server logs, and benchmark JSON with raw latency samples go under `reports/evidence/`. Preserve these files after cleanup. A proof must include the real binary's `READY driver=io_uring` line, the harness actions and assertions, final result, checker negative-oracle result, and controller queue/admission snapshots. Screenshots are not relevant to this headless service. Results apply only to the runner and settings recorded in the evidence; they do not establish a grader target or universal tail-latency claim.

## Cleanup

All scripts own each server PID they launch and terminate/reap it in `finally` blocks, escalating to a kill only for that PID after the bounded timeout. The protocol verifier writes `verification.json` before launch and atomically updates it with `passed: false` on failure, owned PIDs, logs, and cleanup outcomes. It preserves `reports/evidence/`; do not delete evidence as cleanup. If cleanup cannot reap a child, fail the run and report the PID rather than claiming proof complete.

## Feature map

See `features/README.md`; update the matching feature record when a user-visible protocol or admission path changes. Use the `/maintain-verification-skill` workflow when the map or driver drifts.
