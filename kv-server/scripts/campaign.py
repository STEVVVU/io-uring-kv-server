"""Real io_uring service campaign; each policy uses the same frozen offers."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import threading
import time

from verify import start_server, stop_server


def process_sample(pid):
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(') ', 1)[1].split()
        status = Path(f'/proc/{pid}/status').read_text()
        return dict(pid=pid, cpu_ticks=int(fields[11]) + int(fields[12]),
                    rss_kib=int(next(line for line in status.splitlines() if line.startswith('VmRSS:')).split()[1]))
    except (FileNotFoundError, ProcessLookupError, StopIteration):
        return None


def trial(args, root, mode, size, clients, rate, seconds, repeat, profile=False):
    directory = root / f'{size}-{clients}-{rate}-{repeat}-{mode}'
    directory.mkdir()
    record = dict(mode=mode, bytes=size, clients=clients, rate=rate, seconds=seconds,
                  repeat=repeat, passed=False, resources=[])
    server = client = log = None
    done = threading.Event()
    try:
        server, log, port, _ = start_server(args.binary, directory, mode, 32, 0)
        command = [str(args.client), '-address', f'127.0.0.1:{port}', '-bytes', str(size),
                   '-clients', str(clients), '-rate', str(rate), '-seconds', str(seconds), '-seed', '20261022']
        record['command'] = command
        if profile:
            # Separate diagnostic execution; never mix traced samples into timings.
            command = ['strace', '-f', '-c', '-p', str(server.pid)]
            tracer = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=(directory / 'profile.txt').open('wb'))
        else:
            tracer = None
        def capture():
            while not done.wait(.1):
                record['resources'].append(dict(unix_ns=time.time_ns(), monotonic=time.monotonic(), server=process_sample(server.pid),
                                                client=process_sample(client.pid) if client else None))
        monitor = threading.Thread(target=capture, daemon=True)
        monitor.start()
        with (directory / 'client.json').open('wb') as output, (directory / 'client.stderr').open('wb') as error:
            client = subprocess.Popen(record['command'], stdout=output, stderr=error)
            client.wait(timeout=seconds + 120)
            assert client.returncode == 0, f'client exited {client.returncode}'
        record['client'] = json.loads((directory / 'client.json').read_text())
        counts = record['client']['stats']
        assert counts['Offered'] == sum(counts[key] for key in ('Completed', 'Rejected', 'Failed', 'TimedOut', 'Dropped'))
        assert counts['Started'] == counts['Offered'] - counts['Dropped']
        assert counts['Completed'] == record['client']['latency']['end_to_end']['count']
        record['passed'] = counts['Failed'] == counts['TimedOut'] == 0
        record['zero_loss'] = record['passed'] and counts['Rejected'] == counts['Dropped'] == 0
    except BaseException as error:
        record['error'] = repr(error)
    finally:
        done.set()
        if 'monitor' in locals():
            monitor.join(timeout=2)
        record['cleanup_errors'] = []
        for child in (client, locals().get('tracer')):
            try:
                if child and child.poll() is None:
                    child.kill(); child.wait(timeout=5)
            except Exception as error:
                record['cleanup_errors'].append(repr(error))
        if profile and locals().get('tracer'):
            record['profile_exit'] = tracer.returncode
            record['profile_text'] = (directory / 'profile.txt').read_text(errors='replace')
            record['profile_blocked'] = any(text in record['profile_text'] for text in ('ptrace', 'Operation not permitted'))
        try:
            if server:
                record['server_final_resource'] = process_sample(server.pid)
                stop_server(server, log)
        except Exception as error:
            record['cleanup_errors'].append(repr(error))
        record['cleanup'] = not record['cleanup_errors'] and (not server or server.poll() is not None) and (not client or client.poll() is not None)
        (directory / 'result.json').write_text(json.dumps(record, indent=2) + '\n')
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--client', type=Path, required=True)
    parser.add_argument('--bytes', type=int, choices=(64, 1024, 16384), default=64)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--pilot', action='store_true')
    parser.add_argument('--profile', action='store_true')
    args = parser.parse_args()
    args.binary, args.client = args.binary.resolve(), args.client.resolve()
    assert platform.system() == 'Linux', 'native io_uring campaign requires Linux'
    assert shutil.disk_usage('.').free >= 10 * 1024**3, '10 GiB disk reserve required'
    args.output.mkdir(parents=True, exist_ok=False)
    result = dict(passed=False, source_sha=subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
                  dirty=subprocess.check_output(['git', 'status', '--porcelain'], text=True).strip(),
                  platform=platform.platform(), clock_ticks=os.sysconf('SC_CLK_TCK'),
                  binary_sha256=hashlib.sha256(args.binary.read_bytes()).hexdigest(),
                  client_sha256=hashlib.sha256(args.client.read_bytes()).hexdigest(), trials=[])
    for command in (['lscpu'], ['free', '-b'], ['uptime'], ['go', 'version'], ['rustc', '--version']):
        (args.output / (command[0] + '.txt')).write_text(subprocess.check_output(command, text=True))
    size = args.bytes
    rates = {64: (2000, 10000, 50000), 1024: (1000, 5000, 20000), 16384: (500, 2000, 8000)}[size]
    try:
        for repeat in range(1 if args.pilot or args.profile else 5):
            for clients in ((8, 48) if not args.profile else (48,)):
                for rate in (rates if not args.profile else rates[:1]):
                    for mode in (('fixed', 'adaptive') if repeat % 2 == 0 else ('adaptive', 'fixed')):
                        row = trial(args, args.output, mode, size, clients, rate,
                                    2 if args.pilot else 5 if args.profile else 10, repeat, args.profile)
                        result['trials'].append(row)
                        (args.output / 'summary.json').write_text(json.dumps(result, indent=2) + '\n')
                        assert row['cleanup'] and not row.get('error'), row.get('error', 'cleanup failed')
        if not args.pilot and not args.profile:
            # Same conservative rate in both policies, independent of best observed cell.
            for mode in ('adaptive', 'fixed'):
                result['trials'].append(trial(args, args.output, mode, size, 48, rates[0], 120, 'stability'))
        result['passed'] = all(row['passed'] and row['cleanup'] for row in result['trials'])
    finally:
        (args.output / 'summary.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(dict(passed=result['passed'], output=str(args.output))))
    if not result['passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
