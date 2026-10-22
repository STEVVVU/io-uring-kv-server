# Monoio KV service

This package adds an original length-prefixed TCP key-value service to the preserved Monoio runtime checkout. It binds to loopback by default and constructs `RuntimeBuilder<IoUringDriver>` directly. On unsupported kernels or hosts, startup returns the kernel error; there is no epoll fallback. The binary refuses to run outside Linux.

The service is single-owner: all storage operations run on Monoio's local execution thread, and each request mutates or snapshots the map without awaiting. Each `mput` validates its entire batch before changing the map, so it is all-or-nothing. `mget` reads every requested key from one state. Connection tasks share the owner-local map and yield naturally at network reads/writes. This service uses one execution core; it makes no multicore scaling or production availability claim.

Frames are a 4-byte big-endian payload length followed by one request. Requests use opcode `1` GET (`u16 key length`, key), `2` PUT (`u16 key length`, key, `u32 value length`, value), `3` MGET (`u16 count`, repeated keys), or `4` MPUT (`u16 count`, repeated key/value pairs). Keys are nonempty and at most 1 KiB; values are at most 16 KiB; a frame is at most 1 MiB; a batch contains 1–32 items. Stored key/value payload is capped at 64 MiB and 100,000 keys, so memory use remains bounded; writes beyond either cap return status 2 without mutation. The process accepts at most 64 concurrent connections. Responses have a 4-byte length and a status byte: `0` success, `1` malformed/invalid request, `2` overloaded operation. GET and MGET append `u32` value lengths and value bytes; `0xffffffff` means missing. PUT and MPUT return only the status byte. Invalid request payloads receive status 1; truncated or oversized frames close the connection. Disconnects drop the connection task and its socket.

The SEDA-inspired controller samples accepted/rejected connections in windows of 32. In adaptive mode, rejection over 25% cuts the connection limit by one eighth (minimum one), while at most one rejection raises it by one sixteenth, capped at the configured ceiling. The controller also sets the accepted MGET/MPUT item cap from the pending-response snapshot (32, 24, 16, or 8 as occupancy rises); a request over that current cap is rejected before it touches storage. Fixed mode keeps the configured item cap and connection ceiling. These are bounded admission and batch controls informed by queue occupancy, not a reproduction of SEDA or a universal tail-latency guarantee.

Build and run on Linux with a kernel and process policy that permits io_uring:

```sh
cargo build --release -p monoio-kv-server
target/release/monoio-kv-server --bind 127.0.0.1:9000 --batch-mode adaptive
```

On Windows, run only the portable storage/protocol tests with `cargo test -p monoio-kv-server`; the binary exits with an explicit Linux/io_uring requirement. The project-local driver is `.agents/skills/verify/SKILL.md`. Run the real-product check and load comparison on Linux:

```sh
python kv-server/scripts/doctor.py --binary target/release/monoio-kv-server --evidence reports/evidence
python kv-server/scripts/verify.py --binary target/release/monoio-kv-server --evidence reports/evidence
python kv-server/scripts/benchmark.py --binary target/release/monoio-kv-server --evidence reports/evidence
```

The benchmark interleaves two fixed and two adaptive runs using the same seeded offered-operation sequence. It drains all Poisson arrivals due on each scheduler tick, caps the pending client queue, and records offered, actually-started, completed, server/client rejected, timed-out and failed operations separately. It reports wire latency, client queue delay, and offer-to-completion latency independently, with raw samples, p50/p95/p99, throughput, concurrency, and controller logs. Client sockets are closed after worker shutdown. Do not interpret the results as a university grader target or as a general hardware SLO.

The larger [186-trial native campaign](../docs/benchmarks/linux-39730624965/README.md) adds five alternating repeats at each size/rate/concurrency and a 120-second run per policy and size. At 48 clients, fixed completed every offer at the highest tested 50k/20k/8k offers/s for 64 B/1 KiB/16 KiB. Adaptive completed every offer in all five repeats at 10k/5k/8k respectively; higher small-value cells retained explicit server rejections. The [root README](../README.md#measured-results) gives tails and scope; the full matrix retains generator drops, sampled CPU/RSS, environment and raw hashes. No general adaptive speedup is established.

The KV service is original code scoped to a public distributed-systems topic. It does not include or imitate course starter repositories, private handouts, graders, or published student solutions. Relevant topic and inspiration sources:

- Brown CSCI 1675 Fall 2025 Project 2 topic: <https://cs.brown.edu/courses/csci1675/fall2025/hw/project-2.html>
- Welsh, Culler, Brewer, “SEDA: An Architecture for Well-Conditioned, Scalable Internet Services” (SOSP 2001): <https://www.sosp.org/2001/papers/welsh.pdf>
