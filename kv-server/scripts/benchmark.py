#!/usr/bin/env python3
"""Bounded Poisson offered-load comparison with scheduled-arrival accounting."""
import argparse
import json
import random
import socket
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from verify import blob, endpoint, field, mget, mput, recv_exact, start_server, stop_server

TLS = threading.local()
KEYS = [f"k{i:02}".encode() for i in range(32)]
MAX_SCHEDULE_OFFERS = 500_000


def rpc(sock, payload):
    started = time.perf_counter_ns()
    sock.sendall(struct.pack("!I", len(payload)) + payload)
    size = struct.unpack("!I", recv_exact(sock, 4))[0]
    response = recv_exact(sock, size)
    return response, time.perf_counter_ns() - started


def initialize(port, registry, registry_lock):
    sock = endpoint(socket.socket(), port)
    sock.settimeout(2)
    TLS.sock = sock
    with registry_lock:
        registry.append(sock)


def payload_for(rng):
    choice = rng.random()
    if choice < 0.2:
        return mget(KEYS[:16])
    key = rng.choice(KEYS)
    if choice < 0.6:
        return b"\x02" + field(key) + blob(str(rng.randrange(1_000_000)).encode())
    return b"\x01" + field(key)


def build_schedule(seconds, rate, seed):
    """Precompute identical (scheduled offset, request) events for every mode."""
    arrivals = random.Random(seed)
    operations = random.Random(seed ^ 0x5EED5EED)
    duration_ns = int(seconds * 1_000_000_000)
    offset_ns = 0
    schedule = []
    while offset_ns < duration_ns:
        schedule.append((offset_ns, payload_for(operations)))
        offset_ns += max(1, int(arrivals.expovariate(rate) * 1_000_000_000))
        if len(schedule) > MAX_SCHEDULE_OFFERS:
            raise ValueError(f"offered schedule exceeds the {MAX_SCHEDULE_OFFERS} operation safety cap")
    return schedule


def due_arrivals(schedule, next_index, now_offset_ns):
    """Drain every scheduled arrival due at a supplied clock value."""
    end = next_index
    while end < len(schedule) and schedule[end][0] <= now_offset_ns:
        end += 1
    return schedule[next_index:end], end


def schedule_self_test():
    """Fake-clock regression: paused scheduling catches up without retiming offers."""
    schedule = [(0, b"a"), (10, b"b"), (20, b"c"), (30, b"d")]
    due, next_index = due_arrivals(schedule, 0, 25)
    assert due == schedule[:3] and next_index == 3
    caught_up, final_index = due_arrivals(schedule, next_index, 100)
    assert caught_up == schedule[3:] and final_index == len(schedule)
    assert [event[0] for event in due + caught_up] == [0, 10, 20, 30]
    assert build_schedule(0.05, 100, 7) == build_schedule(0.05, 100, 7)


def operation(payload, scheduled_at_perf_ns, enqueued_at_perf_ns, started_jobs, started_lock):
    worker_started_ns = time.perf_counter_ns()
    with started_lock:
        started_jobs.append(scheduled_at_perf_ns)
    response, wire_ns = rpc(TLS.sock, payload)
    completed_at_ns = time.perf_counter_ns()
    return {
        "status": response[0],
        "wire_ms": wire_ns / 1_000_000,
        "scheduler_lag_ms": (enqueued_at_perf_ns - scheduled_at_perf_ns) / 1_000_000,
        "queue_ms": (worker_started_ns - enqueued_at_perf_ns) / 1_000_000,
        "offer_to_completion_ms": (completed_at_ns - scheduled_at_perf_ns) / 1_000_000,
        "completed_at_ns": completed_at_ns,
    }


def percentile(samples, p):
    if not samples:
        return None
    return sorted(samples)[max(0, (len(samples) * p + 99) // 100 - 1)]


def stats(samples):
    return {"p50": percentile(samples, 50), "p95": percentile(samples, 95), "p99": percentile(samples, 99)}


def run_mode(binary, evidence, mode, run, seconds, concurrency, rate, seed, schedule):
    process, log, port, log_path = start_server(binary, evidence, mode, 32, run)
    offered = locally_rejected = completed = completed_in_offer_window = 0
    rejected = timeouts = errors = 0
    wire_samples, scheduler_lag_samples, queue_samples, total_samples = [], [], [], []
    pending = set()
    registry, registry_lock = [], threading.Lock()
    started_jobs, started_lock = [], threading.Lock()
    barrier = threading.Barrier(concurrency)
    try:
        with endpoint(socket.socket(), port) as seed_sock:
            response, _ = rpc(seed_sock, mput({key: b"0" for key in KEYS}))
            if response != b"\x00":
                raise RuntimeError(f"could not seed benchmark: {response!r}")

        with ThreadPoolExecutor(max_workers=concurrency, initializer=initialize,
                                initargs=(port, registry, registry_lock)) as pool:
            warm = [pool.submit(barrier.wait, 5) for _ in range(concurrency)]
            for future in warm:
                future.result(timeout=6)

            # Capture both epochs once; map scheduled monotonic deadlines to perf_counter.
            monotonic_start_ns = time.monotonic_ns()
            perf_start_ns = time.perf_counter_ns()
            offer_window_ns = int(seconds * 1_000_000_000)
            offer_end_monotonic_ns = monotonic_start_ns + offer_window_ns
            offer_end_perf_ns = perf_start_ns + offer_window_ns
            next_index = 0

            while time.monotonic_ns() < offer_end_monotonic_ns:
                now_offset_ns = time.monotonic_ns() - monotonic_start_ns
                due, next_index = due_arrivals(schedule, next_index, now_offset_ns)
                for offset_ns, payload in due:
                    offered += 1
                    scheduled_at = perf_start_ns + offset_ns
                    if len(pending) < concurrency * 2:
                        enqueued_at = time.perf_counter_ns()
                        pending.add(pool.submit(operation, payload, scheduled_at, enqueued_at, started_jobs, started_lock))
                    else:
                        locally_rejected += 1

                done = {future for future in pending if future.done()}
                for future in done:
                    pending.remove(future)
                    try:
                        result = future.result()
                        wire_samples.append(result["wire_ms"])
                        scheduler_lag_samples.append(result["scheduler_lag_ms"])
                        queue_samples.append(result["queue_ms"])
                        total_samples.append(result["offer_to_completion_ms"])
                        if result["status"] == 0:
                            completed += 1
                            completed_in_offer_window += result["completed_at_ns"] <= offer_end_perf_ns
                        elif result["status"] == 2:
                            rejected += 1
                        else:
                            errors += 1
                    except (TimeoutError, socket.timeout):
                        timeouts += 1
                    except Exception:
                        errors += 1
                time.sleep(min(0.001, max(0, (offer_end_monotonic_ns - time.monotonic_ns()) / 1_000_000_000)))

            # Drain scheduled arrivals inside the offer window even if the scheduler woke late.
            due, next_index = due_arrivals(schedule, next_index, offer_window_ns - 1)
            for offset_ns, payload in due:
                offered += 1
                scheduled_at = perf_start_ns + offset_ns
                if len(pending) < concurrency * 2:
                    enqueued_at = time.perf_counter_ns()
                    pending.add(pool.submit(operation, payload, scheduled_at, enqueued_at, started_jobs, started_lock))
                else:
                    locally_rejected += 1

            drain_deadline = time.monotonic() + 10
            for future in as_completed(pending, timeout=max(0, drain_deadline - time.monotonic())):
                pending.remove(future)
                try:
                    result = future.result()
                    wire_samples.append(result["wire_ms"])
                    scheduler_lag_samples.append(result["scheduler_lag_ms"])
                    queue_samples.append(result["queue_ms"])
                    total_samples.append(result["offer_to_completion_ms"])
                    if result["status"] == 0:
                        completed += 1
                        completed_in_offer_window += result["completed_at_ns"] <= offer_end_perf_ns
                    elif result["status"] == 2:
                        rejected += 1
                    else:
                        errors += 1
                except (TimeoutError, socket.timeout):
                    timeouts += 1
                except Exception:
                    errors += 1
            end_perf_ns = time.perf_counter_ns()
    except TimeoutError:
        for future in pending:
            future.cancel()
        raise
    finally:
        for sock in registry:
            sock.close()
        stop_server(process, log)

    with started_lock:
        attempted = len(started_jobs)
    elapsed_including_drain = (end_perf_ns - perf_start_ns) / 1_000_000_000
    completion_drain_seconds = max(0, end_perf_ns - offer_end_perf_ns) / 1_000_000_000
    return {
        "run": run, "mode": mode, "offer_window_seconds": seconds,
        "schedule_offers": len(schedule), "offered": offered,
        "enqueued": offered - locally_rejected, "attempted": attempted,
        "completed": completed, "completed_during_offer_window": completed_in_offer_window,
        "server_rejected": rejected, "client_queue_rejected": locally_rejected,
        "timeouts": timeouts, "errors": errors,
        "concurrency": concurrency, "offered_rate_per_second": rate, "seed": seed,
        "elapsed_including_drain_seconds": elapsed_including_drain,
        "completion_drain_seconds": completion_drain_seconds,
        "offered_rate_observed_per_second": offered / seconds,
        "completion_throughput_including_drain_per_second": completed / elapsed_including_drain,
        "wire_latency_ms": stats(wire_samples),
        "scheduler_lag_ms": stats(scheduler_lag_samples),
        "queue_delay_ms": stats(queue_samples),
        "offer_to_completion_latency_ms": stats(total_samples),
        "raw_wire_latency_ms": wire_samples,
        "raw_scheduler_lag_ms": scheduler_lag_samples,
        "raw_queue_delay_ms": queue_samples,
        "raw_offer_to_completion_latency_ms": total_samples,
        "controller_log": str(log_path),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, default=Path("reports/evidence"))
    parser.add_argument("--seconds", type=float, default=3.0)
    parser.add_argument("--concurrency", type=int, default=48)
    parser.add_argument("--offered-rate", type=float, default=1200.0)
    args = parser.parse_args()
    if not 0 < args.seconds <= 60 or not 1 <= args.concurrency <= 64 or not 0 < args.offered_rate <= 100_000:
        parser.error("seconds must be in (0, 60], rate in (0, 100000], concurrency in 1..=64")
    binary = args.binary.resolve()
    if not binary.is_file():
        parser.error(f"binary does not exist: {binary}")
    try:
        schedule = build_schedule(args.seconds, args.offered_rate, 20261021)
    except ValueError as error:
        parser.error(str(error))
    schedule_self_test()
    args.evidence.mkdir(parents=True, exist_ok=True)
    results = [
        run_mode(binary, args.evidence, mode, run, args.seconds, args.concurrency,
                 args.offered_rate, 20261021, schedule)
        for run, mode in enumerate(("fixed", "adaptive", "adaptive", "fixed"), start=1)
    ]
    output = args.evidence / "benchmark.json"
    output.write_text(json.dumps({"runs": results}, indent=2) + "\n", encoding="utf-8")
    for result in results:
        print(json.dumps({key: value for key, value in result.items() if not key.startswith("raw_")}, sort_keys=True))
    print(f"Raw samples and summaries: {output}")


if __name__ == "__main__":
    main()
