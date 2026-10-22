# Repository guidance

Preserve the complete Monoio source/history and its MIT/Apache-2.0 notices. The added product is `kv-server`, an original loopback-only, in-memory Linux io_uring service. Do not substitute an epoll fallback or change system/kernel policy to make verification pass.

Use Rust 1.99.1 and committed Cargo.lock. Commands: `cargo fmt --all -- --check`, `cargo test --locked -p monoio-kv-server`, `cargo clippy --locked --no-deps -p monoio-kv-server --all-targets -- -D warnings`, `cargo build --locked --release -p monoio-kv-server`. Windows portable checks cannot prove io_uring behavior.

The maintained real-product driver and feature map are documented in `.agents/skills/verify/SKILL.md`. Run Doctor, Drive and the intentional failure cleanup check on native Linux. Keep run evidence under ignored `reports/evidence/`, separate from maintained scripts in `kv-server/scripts/`. Stop and reap only driver-owned processes. Preserve failing evidence and distinguish benchmark wire latency from queue and end-to-end latency.

All addresses must remain numeric loopback addresses; requests are unauthenticated. Retain frame/key/value/batch/store/connection bounds and whole-batch validation before mutation. Do not claim disk durability, multicore throughput, a course grading SLO or a general performance win from a single run.

Inherited workflows are retained outside the executable workflow directory. New checks require only read access and must not publish, deploy, message others or change account settings.
