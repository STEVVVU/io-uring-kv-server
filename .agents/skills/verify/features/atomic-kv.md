# Atomic KV operations

## Sub-features

GET, PUT, MGET snapshot reads, and all-or-nothing MPUT batches.

## How to get to it (user POV)

Connect to the server's loopback TCP endpoint and send the documented length-prefixed request opcodes.

## Driving it with the Python TCP verification harness

Run `python kv-server/scripts/verify.py --binary target/release/monoio-kv-server --evidence reports/evidence`. The harness performs real writes and reads, drives concurrent MPUT/MGET histories, and checks those intervals independently against the sequential map specification.

## Gotchas

The server owns the map on one local execution thread. MGET/MPUT batches may be rejected by the adaptive pending-response cap before storage access.
