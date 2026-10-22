# Fixed and adaptive load control

## Sub-features

Fixed batch ceiling; adaptive active-connection batch cap and rejection-feedback connection admission; offered/completed load accounting and latency percentiles.

## How to get to it (user POV)

Start the server with `--batch-mode fixed` or `--batch-mode adaptive`, then drive the TCP workload.

## Driving it with the Python TCP verification harness

Run `python kv-server/scripts/benchmark.py --binary target/release/monoio-kv-server --evidence reports/evidence`. It interleaves both modes and keeps raw latency samples and server controller logs.

## Gotchas

An adaptive request batch larger than the current active-connection cap receives status 2 without changing storage. Benchmark results depend on the runner and are not a universal SLO.
