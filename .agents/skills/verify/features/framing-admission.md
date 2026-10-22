# Framing and connection limits

## Sub-features

Partial header/body reads, complete writes, invalid and oversized frames, disconnect cleanup, bounded active connections, and progress under a non-reading peer.

## How to get to it (user POV)

Open TCP connections to the loopback listener and send length-prefixed frames.

## Driving it with the Python TCP verification harness

Run `python kv-server/scripts/verify.py --binary target/release/monoio-kv-server --evidence reports/evidence`. It sends split request bytes, truncated/oversized traffic, fills a peer's send queue with large MGET responses, and tests rejection and recovery at a one-connection cap.

## Gotchas

Truncated and oversized frames close their connection. An accepted socket beyond the active limit is closed immediately. The service is loopback-only and has no authentication.
