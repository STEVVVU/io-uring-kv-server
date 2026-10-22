#!/usr/bin/env python3
"""Drive the Linux binary through its public TCP protocol and keep proof logs."""
import argparse
import contextlib
import json
import socket
import struct
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

OK, BAD_REQUEST, OVERLOADED = 0, 1, 2
MAX_FRAME = 1 << 20


def frame(payload):
    return struct.pack("!I", len(payload)) + payload


def field(data):
    return struct.pack("!H", len(data)) + data


def blob(data):
    return struct.pack("!I", len(data)) + data


def mput(values):
    return b"\x04" + struct.pack("!H", len(values)) + b"".join(
        field(key) + blob(value) for key, value in values.items()
    )


def mget(keys):
    return b"\x03" + struct.pack("!H", len(keys)) + b"".join(field(key) for key in keys)


def recv_exact(sock, size):
    out = bytearray()
    while len(out) < size:
        part = sock.recv(size - len(out))
        if not part:
            raise EOFError("connection closed in frame")
        out.extend(part)
    return bytes(out)


def rpc(sock, payload):
    started = time.perf_counter_ns()
    sock.sendall(frame(payload))
    size = struct.unpack("!I", recv_exact(sock, 4))[0]
    if not 1 <= size <= MAX_FRAME:
        raise AssertionError(f"invalid response size: {size}")
    response = recv_exact(sock, size)
    ended = time.perf_counter_ns()
    return response, started, ended


def endpoint(sock, port):
    sock.settimeout(2)
    sock.connect(("127.0.0.1", port))
    return sock


def decode_mget(response, keys):
    assert response[0] == OK, response
    at = 1
    result = {}
    for key in keys:
        size = struct.unpack_from("!I", response, at)[0]
        at += 4
        json_key = key.decode("utf-8")
        if size == 0xFFFFFFFF:
            result[json_key] = None
        else:
            result[json_key] = response[at:at + size].decode("utf-8")
            at += size
    assert at == len(response)
    return result


def linearizable(history):
    """Independent sequential-spec backtracking checker for put/mput/get/mget."""
    count = len(history)
    predecessors = [0] * count
    for later, b in enumerate(history):
        for earlier, a in enumerate(history):
            if a["end"] <= b["start"]:
                predecessors[later] |= 1 << earlier

    failed = set()

    def visit(done, state):
        if done == (1 << count) - 1:
            return True
        key = (done, tuple(sorted(state.items())))
        if key in failed:
            return False
        for index, op in enumerate(history):
            bit = 1 << index
            if done & bit or predecessors[index] & ~done:
                continue
            next_state = dict(state)
            if op["kind"] == "mput":
                next_state.update(op["values"])
            elif op["kind"] == "mget":
                observed = {k: next_state.get(k) for k in op["keys"]}
                if observed != op["result"]:
                    continue
            if visit(done | bit, next_state):
                return True
        failed.add(key)
        return False

    return visit(0, {})


def checker_self_test():
    assert decode_mget(b"\x00\x00\x00\x00\x01A\x00\x00\x00\x01A", [b"x", b"y"]) == {"x": "A", "y": "A"}
    valid = [
        {"kind": "mput", "values": {"x": "A", "y": "A"}, "start": 1, "end": 5},
        {"kind": "mget", "keys": ["x", "y"], "result": {"x": "A", "y": "A"}, "start": 2, "end": 4},
    ]
    fractured = [
        {"kind": "mput", "values": {"x": "A", "y": "A"}, "start": 1, "end": 2},
        {"kind": "mput", "values": {"x": "B", "y": "B"}, "start": 3, "end": 4},
        {"kind": "mget", "keys": ["x", "y"], "result": {"x": "A", "y": "B"}, "start": 5, "end": 6},
    ]
    assert linearizable(valid)
    assert not linearizable(fractured), "checker accepted a fractured snapshot"


def exercise(port):
    # Basic operations, binary values, and split writes exercise read_exact/write_all.
    with endpoint(socket.socket(), port) as sock:
        seed = mput({b"x": b"seed", b"y": b"seed"})
        packet = frame(seed)
        seed_start = time.perf_counter_ns()
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        for part in (packet[:1], packet[1:3], packet[3:8], packet[8:]):
            sock.sendall(part)
            time.sleep(0.01)
        seed_response = recv_exact(sock, 5)
        seed_end = time.perf_counter_ns()
        assert seed_response == b"\x00\x00\x00\x01\x00"
        value, _, _ = rpc(sock, b"\x01" + field(b"missing"))
        assert value == b"\x00\xff\xff\xff\xff"
        value, _, _ = rpc(sock, b"\x02" + field(b"roundtrip") + blob(b"value-check"))
        assert value == bytes([OK])
        value, _, _ = rpc(sock, b"\x01" + field(b"roundtrip"))
        assert value == bytes([OK]) + blob(b"value-check")
        value, _, _ = rpc(sock, mget([b"x", b"y"]))
        assert decode_mget(value, [b"x", b"y"]) == {"x": "seed", "y": "seed"}
        value, _, _ = rpc(sock, b"\xff")
        assert value == bytes([BAD_REQUEST])

    # Concurrent atomic batches and snapshots; independently check real-time order.
    history = [{
        "kind": "mput", "values": {"x": "seed", "y": "seed"},
        "start": seed_start, "end": seed_end,
    }]
    lock = threading.Lock()
    barrier = threading.Barrier(4)

    def writer(prefix):
        with endpoint(socket.socket(), port) as sock:
            barrier.wait()
            for i in range(3):
                values = {"x": f"{prefix}{i}", "y": f"{prefix}{i}"}
                response, start, end = rpc(sock, mput({k.encode(): v.encode() for k, v in values.items()}))
                assert response == bytes([OK])
                with lock:
                    history.append({"kind": "mput", "values": values, "start": start, "end": end})

    def reader():
        with endpoint(socket.socket(), port) as sock:
            barrier.wait()
            for _ in range(3):
                response, start, end = rpc(sock, mget([b"x", b"y"]))
                values = decode_mget(response, [b"x", b"y"])
                assert values["x"] == values["y"], f"fractured mget: {values}"
                with lock:
                    history.append({"kind": "mget", "keys": ["x", "y"], "result": values, "start": start, "end": end})

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(writer, "a"), pool.submit(writer, "b"), pool.submit(reader), pool.submit(reader)]
        for future in futures:
            future.result(timeout=10)
    assert linearizable(history), "real concurrent operation history is not linearizable"

    # Half-closed and oversized frames must release the session without allocation/loops.
    with endpoint(socket.socket(), port) as truncated:
        truncated.sendall(b"\x00\x00")
    with endpoint(socket.socket(), port) as oversized:
        oversized.sendall(struct.pack("!I", MAX_FRAME + 1))
        assert oversized.recv(1) == b""

    # Fill the socket send queue with large replies while another peer remains responsive.
    bulk = {f"bulk-{i}".encode(): b"v" * (16 << 10) for i in range(32)}
    with endpoint(socket.socket(), port) as sock:
        assert rpc(sock, mput(bulk))[0] == bytes([OK])
    request = frame(mget(list(bulk)))
    with endpoint(socket.socket(), port) as stalled:
        for _ in range(64):
            stalled.sendall(request)
        with endpoint(socket.socket(), port) as healthy:
            response, _, _ = rpc(healthy, mget([b"x"]))
            assert decode_mget(response, [b"x"])["x"] is not None
    return {"concurrent_history": history, "fractured_snapshot_negative_oracle": "rejected", "stalled_peer_payload_bytes": len(request) * 64}


def capacity_check(binary, evidence, record, persist):
    port = free_port()
    log = (evidence / "capacity-server.log").open("wb")
    process = subprocess.Popen([str(binary), "--bind", f"127.0.0.1:{port}", "--max-connections", "1", "--batch-mode", "fixed"], stdout=log, stderr=subprocess.STDOUT)
    owner = {"pid": process.pid, "mode": "capacity", "log": str(evidence / "capacity-server.log"), "cleanup": "pending"}
    record["owned_processes"].append(owner)
    try:
        persist()
        deadline = time.monotonic() + 10
        while True:
            if process.poll() is not None:
                raise RuntimeError(f"capacity server exited before readiness: {process.returncode}")
            candidate = socket.socket()
            try:
                first = endpoint(candidate, port)
                break
            except ConnectionRefusedError:
                candidate.close()
                if time.monotonic() >= deadline:
                    raise TimeoutError("capacity server did not begin listening")
                time.sleep(0.005)
            except BaseException:
                candidate.close()
                raise
        with first:
            response, _, _ = rpc(first, mget([b"x"]))
            assert response[0] == OK, "first connection was not acknowledged"
            with endpoint(socket.socket(), port) as second:
                assert second.recv(1) == b"", "over-capacity connection remained open"
        deadline = time.monotonic() + 5
        recovered = False
        last_error = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"capacity server exited early with code {process.returncode}")
            try:
                with endpoint(socket.socket(), port) as third:
                    response, _, _ = rpc(third, mget([b"x"]))
                    if response[0] == OK:
                        recovered = True
                        break
            except (ConnectionError, EOFError, OSError) as error:
                last_error = repr(error)
            time.sleep(0.005)
        assert recovered, f"capacity did not recover after active peer closed: {last_error}"
    finally:
        try:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            owner["cleanup"] = "reaped"
        except BaseException as error:
            owner["cleanup"] = "failed"
            owner["cleanup_error"] = repr(error)
            raise
        finally:
            log.close()
            record["cleanup"].append({"pid": process.pid, "result": owner["cleanup"]})
            persist()


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def bind_rejection_check(binary):
    result = subprocess.run(
        [str(binary), "--bind", "0.0.0.0:0"], capture_output=True, text=True, timeout=10
    )
    assert result.returncode != 0, "server accepted an externally reachable bind address"
    assert "loopback" in result.stderr, f"unexpected bind rejection: {result.stderr!r}"


def idle_peer_batch_check(port):
    """Idle keep-alive peers must not masquerade as queued response work."""
    values = {f"idle-{i}".encode(): f"value-{i}".encode() for i in range(16)}
    with endpoint(socket.socket(), port) as seed:
        assert rpc(seed, mput(values))[0] == bytes([OK])
    with contextlib.ExitStack() as peers:
        sockets = []
        for _ in range(48):
            sock = peers.enter_context(endpoint(socket.socket(), port))
            assert decode_mget(rpc(sock, mget([b"idle-0"]))[0], [b"idle-0"])["idle-0"] == values[b"idle-0"].decode()
            sockets.append(sock)
        response = rpc(sockets[0], mget(list(values)))[0]
        assert decode_mget(response, list(values)) == {k.decode(): v.decode() for k, v in values.items()}
    return {"idle_persistent_peers": 48, "mget_items": 16, "exact_values": True}


def start_server(binary, evidence, mode, batch, run):
    port = free_port()
    log_path = evidence / f"server-{run}-{mode}.log"
    log = log_path.open("wb")
    args = [str(binary), "--bind", f"127.0.0.1:{port}", "--max-connections", "64", "--batch-mode", mode, "--fixed-batch", str(batch)]
    process = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            log.close()
            raise RuntimeError(f"native io_uring server exited {process.returncode}; see {log_path}")
        try:
            with endpoint(socket.socket(), port):
                break
        except OSError:
            time.sleep(0.05)
    else:
        stop_server(process, log)
        raise TimeoutError(f"server did not accept connections; see {log_path}")
    log.flush()
    output = log_path.read_text(encoding="utf-8", errors="replace")
    if "READY driver=io_uring" not in output:
        stop_server(process, log)
        raise RuntimeError(f"server accepted TCP without confirming native driver; see {log_path}")
    return process, log, port, log_path


def stop_server(process, log):
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)
    log.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, default=Path("reports/evidence"))
    parser.add_argument("--inject-failure-after-launch", action="store_true", help="exercise failure evidence and owned-process cleanup")
    args = parser.parse_args()
    binary = args.binary.resolve()
    if not binary.is_file():
        parser.error(f"binary does not exist: {binary}")
    args.evidence.mkdir(parents=True, exist_ok=True)
    evidence = args.evidence / "verification.json"
    record = {"driver": "io_uring", "passed": False, "proofs": {}, "owned_processes": [], "cleanup": []}

    def persist():
        temporary = evidence.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        temporary.replace(evidence)

    args.evidence.mkdir(parents=True, exist_ok=True)
    persist()
    try:
        checker_self_test()
        bind_rejection_check(binary)
        for run, mode in enumerate(("fixed", "adaptive"), start=1):
            process, log, port, log_path = start_server(binary, args.evidence, mode, 32, run)
            owner = {"pid": process.pid, "mode": mode, "log": str(log_path), "cleanup": "pending"}
            record["owned_processes"].append(owner)
            try:
                persist()
                if args.inject_failure_after_launch:
                    raise AssertionError("intentional failure after launch")
                record["proofs"][mode] = exercise(port)
                record["proofs"][mode]["idle_peer_batch"] = idle_peer_batch_check(port)
            finally:
                try:
                    stop_server(process, log)
                    owner["cleanup"] = "reaped" if process.poll() is not None else "unreaped"
                except BaseException as error:
                    owner["cleanup"] = "failed"
                    owner["cleanup_error"] = repr(error)
                    raise
                finally:
                    record["cleanup"].append({"pid": process.pid, "result": owner["cleanup"]})
                    persist()
        capacity_check(binary, args.evidence, record, persist)
        record["passed"] = True
        persist()
        print(f"PASS: real TCP protocol, concurrency history, limits, partial I/O, and cleanup; evidence={evidence}")
    except BaseException as error:
        record["passed"] = False
        record["failure"] = {"type": type(error).__name__, "message": str(error)}
        persist()
        raise


if __name__ == "__main__":
    main()
