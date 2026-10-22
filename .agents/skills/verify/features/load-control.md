# Fixed and adaptive load control

## Sub-features

Fixed batch ceiling; adaptive active-connection batch cap and rejection-feedback connection admission; offered/completed load accounting and latency percentiles.

## How to get to it (user POV)

Start the server with `--batch-mode fixed` or `--batch-mode adaptive`, then drive the TCP workload.

## Driving it with the Python TCP verification harness

Run `python kv-server/scripts/benchmark.py --binary target/release/monoio-kv-server --evidence reports/evidence`. It interleaves both modes and keeps raw latency samples and server controller logs.

## Gotchas

Use `campaign.py` and `load.go` for the longer scheduled-arrival sweep in
`kv-server/BENCHMARK.md`. Its per-operation outcomes prevent an adaptive
MGET rejection from making a reduced successful request mix look faster.
Compare aggregate latency only for zero-loss cells. Resource samples include
seed/warmup; filter by the client's recorded offering start/end for steady-state
CPU deltas. All raw histograms, errors and owned-process cleanup survive the run.

An adaptive request batch larger than the current active-connection cap receives status 2 without changing storage. Benchmark results depend on the runner and are not a universal SLO.
