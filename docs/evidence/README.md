# Native Linux evidence

`linux-39713710063/` contains the actual successful protocol/history verdict, failed-control verdict, native io_uring startup log and capacity rejection log from [run 39713710063](https://github.com/STEVVVU/io-uring-kv-server/actions/runs/39713710063), testing product commit `7f1cf5dc57a61fd21e330f133b5178752c713939`.

`benchmark-summary.json` removes only raw sample arrays from the downloaded benchmark artifact and retains all summary measurements, original sample artifact SHA-256 and run identity. The full raw artifact is preserved locally in the task evidence store and downloadable from that run while GitHub retains it. Adaptive admission rejected 730/3624 offers in both trials; these measurements do not demonstrate a performance benefit. The service is in-memory, single-core and loopback-only.
