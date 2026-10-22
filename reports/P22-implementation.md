# P22 implementation handoff

## Result

The local checkout is the full Monoio upstream clone, including its original 209-commit history, upstream README, examples, MIT/Apache licenses, and third-party notice. The upstream pin is `8675e9406ea7943c441d969e38c3f5fc198ef6d1` (`origin` remains `https://github.com/monoio-rs/monoio.git`). No upstream source or notice files were changed. The added package is `kv-server/`, registered as the `monoio-kv-server` workspace member, with a workspace `Cargo.lock`.

The new service has its own length-prefixed TCP protocol for GET/PUT/MGET/MPUT. Storage work is owned by one local execution thread; MPUT parses and validates the full batch before applying it, and MGET returns one state snapshot. Frames, keys, values, batch counts, active connections, stored payload bytes, and key count are bounded. Partial reads and writes use Monoio's rent-aware `read_exact` and `write_all`; malformed payloads get a status response, while truncated and oversized frames close their socket. The process is loopback-bound by default, has no authentication or persistence, and is not presented as production-ready.

Linux startup builds `RuntimeBuilder<IoUringDriver>` directly with Monoio's `iouring` feature and no `legacy` feature. Unsupported/denied io_uring setup therefore fails at startup; there is no epoll fallback. The Windows entry point exits with an explicit Linux/io_uring requirement.

The adaptive controller samples accept/reject load in 32-connection windows. It reduces its live admission limit when more than 25% of that window was rejected, raises it when at most one was rejected, and caps both limits at configured bounds. It also adjusts the allowed MGET/MPUT size from active-connection occupancy. Pure controller rules have deterministic boundary tests. This is a small SEDA-inspired queue/admission policy, not a complete SEDA design or a claim of universal latency improvement.

## Durable verification and benchmark setup

- `.agents/skills/verify/SKILL.md` and its feature map define Launch, Doctor, Drive, Evidence, Cleanup, and the top user paths. The skill is explicitly marked **Draft** because it has not yet been driven on a native Linux host.
- `kv-server/scripts/doctor.py` probes an isolated live instance and preserves its actual startup output.
- `kv-server/scripts/verify.py` drives the real public TCP protocol, fragmented client writes, exact GET/PUT value checks, batch atomicity/snapshots, concurrent histories, malformed/truncated/oversized input, a non-reading peer, fairness, connection rejection/recovery, and teardown. The decoder now normalizes keys to JSON-safe UTF-8 strings, with a regression for the reproduced MGET issue. Its independent sequential-spec checker includes a deliberately fractured snapshot that must be rejected. A deliberate failure flag checks false-result evidence and owned-process cleanup.
- The service CLI rejects non-loopback bind addresses, and the verifier checks that boundary on Linux. Unit tests cover valid IPv4/IPv6 loopback literals and invalid wildcard/public/hostname values.
- `kv-server/scripts/benchmark.py` prepares four interleaved fixed/adaptive bounded Poisson runs, drains every arrival due at each scheduler tick, and uses one seeded offered-operation sequence across modes. It reports offered, actually-started, completed, server/client-queue rejected, timed out, and failed operations. Wire latency, client queue delay, and offer-to-completion latency are reported separately with raw samples and p50/p95/p99. Worker sockets are closed explicitly after executor shutdown.
- `.github/workflows/kv-server-linux.yml` is a read-only-permission GitHub-hosted Ubuntu VM job, not a container. It uses pinned checkout/artifact actions and Rust 1.99.1, uses `--locked` on Cargo checks/builds, builds the forced native driver, runs the real-product driver and benchmark, and retains evidence even on failure. Kernel denial/EPERM is visible as a failure and is never counted as a fallback pass. Existing upstream workflows were left byte-for-byte unchanged.

The code is original and scoped to the public distributed-systems topic. It does not include course starter code, private handouts, graders, or published student solutions. Background sources are Brown CSCI 1675 Fall 2025 Project 2 (`https://cs.brown.edu/courses/csci1675/fall2025/hw/project-2.html`) and the SEDA paper (`https://www.sosp.org/2001/papers/welsh.pdf`). The added behavior and benchmark limits are documented in `kv-server/README.md`.

## Checks run here

Toolchain: isolated `coursework-rust` Rust/Cargo 1.99.1 on Windows. An x86_64 Linux standard-library target was added only to that isolated toolchain for compile checking; no global installation, WSL, VM, container, kernel setting, or memlock policy was changed.

- `cargo fmt --all -- --check` — passed (upstream rustfmt configuration prints stable-channel warnings for its existing nightly-only options).
- `cargo test --locked -p monoio-kv-server` — passed; 8 tests cover protocol validation, loopback-only bind parsing, all-or-nothing batches/store byte/key bounds, and adaptive-controller boundaries.
- `cargo check --locked -p monoio-kv-server --target x86_64-unknown-linux-gnu` — passed; this type-checks the Linux `IoUringDriver` path but is not a native runtime proof.
- `cargo clippy --locked --no-deps -p monoio-kv-server --all-targets --target x86_64-unknown-linux-gnu -- -D warnings` — passed.
- `python -m py_compile kv-server/scripts/verify.py kv-server/scripts/doctor.py kv-server/scripts/benchmark.py` — passed.
- Python checker self-test — passed: the reproduced byte-key decoder case returns JSON-safe `{"x": "A", "y": "A"}`, the sequential history is accepted, and the deliberately fractured MGET history is rejected.
- `verify.py --help` and `benchmark.py --help` — passed; the failure-injection control and bounded load flags are exposed.
- `cargo test --locked --workspace --all-targets -- --skip legacy_connect_timeout_dst` — passed across the workspace while excluding only the observed outbound-network-dependent timeout test.
- Baseline `cargo test --workspace --all-targets` — failed in existing `monoio/tests/tcp_connect.rs::legacy_connect_timeout_dst`: the test expects `TcpStream::connect("1.1.1.1:1")` to remain pending for one second, while this environment returns promptly. This failure occurs outside the new package and was observed before product changes.
- Unscoped `cargo clippy -p monoio-kv-server --all-targets --target x86_64-unknown-linux-gnu -- -D warnings` — reaches upstream Monoio but fails on two existing `clippy::question_mark` suggestions in `monoio/src/utils/slab.rs` (lines 50 and 96). The scoped `--no-deps` command above passes; upstream files were left untouched.

## Remaining acceptance gate

This Windows host cannot run the native service, so no Linux runtime, protocol, fairness, capacity, or load result is claimed. The prepared Ubuntu job must run after main reviews the local diff. Its artifact should be inspected for `READY driver=io_uring`, controller queue/rejection snapshots, complete protocol/concurrency evidence, cleanup success, the deliberate failure record, and raw benchmark samples. If hosted io_uring setup is denied, the native proof remains blocked and should be reported as such. No remote repository, branch, push, commit, deployment, or external message was created.
