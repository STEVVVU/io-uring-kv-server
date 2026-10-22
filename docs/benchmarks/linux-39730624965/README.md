# Native Linux campaign 39730624965

Source `59eaa1611fe23678de33b497eea36ede80faccc8`. [Hosted run](https://github.com/STEVVVU/io-uring-kv-server/actions/runs/39730624965). All 186 trials retained in [results.json](results.json), including all 36 five-repeat groups and six 120-second stability trials. All exact response oracles and owned cleanup passed; no client failure or timeout. Separate native verification includes 48 idle peers with a full MGET16.

Each size ran on a separate four-vCPU AMD EPYC 7763 Ubuntu 24.04 Azure runner, Rust 1.99.1 and Go 1.27.3. This is single-runtime-thread, loopback, warm in-memory traffic. Fixed/adaptive pairs share seed 20261022 and alternate order. Cross-job hardware/kernel differences prohibit a precise causal speedup ratio against the earlier run.

## Highest tested lossless rates at 48 clients

Rates below completed every offer across all five ten-second repeats. Values are p99 scheduled-offer-to-validated-response milliseconds, median [min, max]. These are tested lower bounds, not proven capacity maxima.

| Value | Policy | Offers/s | Completed/offered across five repeats | p99 ms median[min,max] |
|---|---|---:|---:|---|
|64B|fixed|50,000|2,503,715/2,503,715|1.606[1.606,1.638]|
|64B|adaptive|10,000|500,080/500,080|1.343[1.343,1.343]|
|1024B|fixed|20,000|1,001,860/1,001,860|1.573[1.540,1.606]|
|1024B|adaptive|5,000|248,745/248,745|1.278[1.278,1.311]|
|16384B|fixed|8,000|399,310/399,310|2.949[2.753,3.539]|
|16384B|adaptive|8,000|399,310/399,310|2.949[2.753,3.277]|

At 64 B / 48 clients / 50k offers/s, adaptive rejected 568 of 2,503,715 offers; fixed completed all. At 1 KiB / 48 clients / 20k, adaptive rejected one of 1,001,860 offers, so it is not a five-repeat lossless cell. With only eight clients, the highest rates dropped work at the generator for both policies. Those losses remain in the full matrix and are not low-latency wins.

At 120 seconds / 48 clients, each policy completed 240,340/240,340 at 64 B / 2k offers/s, 119,963/119,963 at 1 KiB / 1k, and 59,894/59,894 at 16 KiB / 500. Their successful end-to-end p99 was 1.212, 1.212 and 1.573 ms respectively. These stability cells ran once per policy, not five repeats.

At the highest lossless 48-client cells, median sampled server CPU was 83.1%/20.5% of one core for fixed/adaptive at 64 B, 40.5%/12.3% at 1 KiB, and 42.4%/41.5% at 16 KiB. These are different offered rates for the first two sizes. Sampled server RSS stayed below 14.75 MiB across the whole matrix. The client scheduler contributes roughly 1 ms at p99; do not interpret end-to-end latency as pure server latency. Histogram quantiles are upper bucket edges with at most 3.125% width, not exact nanosecond measurements.

## Fix and diagnosis

The old adaptive batch gate counted idle connected peers as pending responses: 48 idle peers forced the batch cap below 16 and rejected every MGET16. Pending occupancy now begins only after a complete valid request and is released after response completion or early return. Connection admission still uses live connections. The native 48-idle-peer regression and all bounds/atomic-history checks passed. This direct execution-path proof establishes the correctness fix; it does not establish a general throughput advantage. Adaptive admission still rejects work under load.

The separate strace attach was denied by runner process policy; its diagnostic log is retained. No security policy was changed and no sampled hot-path attribution is claimed.

## Reproduce

See [BENCHMARK.md](../../../kv-server/BENCHMARK.md) for the frozen workload and build/invocation. Dispatch the Native KV load campaign workflow for the full three-size matrix. The earlier raw run 39729678610 remains separate; do not pool before/after samples. Raw artifacts are available on the hosted run and identified by SHA256 in results.json.
