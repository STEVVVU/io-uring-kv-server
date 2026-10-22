# Native load campaign

Frozen before first measurement: seeded Poisson arrivals, persistent 8/48 clients,
64/1024/16384-byte values, 4096 keys (512 for large values), 40% GET, 40% PUT,
20% MGET16. PUT writes each key's deterministic value so every concurrent response
can be checked exactly without imposing a client-side serialization lock. Existing
verification separately proves changing-value atomic histories. This synthetic
steady-state workload excludes cold connections, variable values, disk and TLS.

Rates are 2000/10000/50000, 1000/5000/20000 and 500/2000/8000 offers/s respectively.
Five ten-second pairs alternate fixed/adaptive order; both policies receive identical
arrival schedules. Two 120-second stability runs use the lowest frozen rate with
48 clients. Pilot uses two seconds and one pair per cell and supports no speedup
claim. Warm connections/seed data are outside the measured offering clock. A bounded
queue of twice concurrency drops excess work explicitly without retiming arrivals.
End-to-end success latency includes scheduling, request generation, queueing and
response validation. Wire latency excludes client generation/queueing. All offers
have exactly one outcome, including rejected, failed, timed-out and dropped work.

The stdlib Go client avoids the previous Python thread-pool/GIL generator ceiling.
It preserves raw 2048-bin latency histograms, exact counts/maxima, and upper-bound
p50/p95/p99 with at most 3.125% bucket width above 32 ns. Tail reports concern
successful requests; rejections are separate, never performance wins. The Python
controller reuses verification launch/cleanup, captures service/client `/proc`
CPU/RSS and source/toolchain/workload identity. Profiles run separately; unavailable
ptrace is reported, never replaced with a host security change. No causal claim
without a corresponding profile or direct execution-path proof.

Build the locked release service and `go build -o /tmp/kv-load kv-server/scripts/load.go`.
Run `go run kv-server/scripts/load.go -self-test`, then
`python3 kv-server/scripts/campaign.py --binary target/release/monoio-kv-server --client /tmp/kv-load --bytes 64 --output reports/evidence/my-run`.
Use the manual Native KV load campaign workflow for isolated Ubuntu runners.
Outputs stay ignored, with retained raw histograms/resources and reaped owned PIDs.
Compare identical frozen cells by median and full range across all five repeats;
generator lag/CPU must leave sufficient headroom before claiming server capacity.
