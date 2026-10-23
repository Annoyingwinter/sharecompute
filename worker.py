#!/usr/bin/env python3
"""Cross-platform ShareCompute worker. Copy this file alone to a worker PC."""
import argparse
import base64
import datetime
import json
import os
from pathlib import Path, PureWindowsPath
import platform
import signal
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request

MAX_FILE = 50 * 1024 * 1024


def log(message):
    print(f'{datetime.datetime.now().astimezone().isoformat(timespec="seconds")} {message}', flush=True)


def request(url, token, path, data):
    body = json.dumps(data).encode('utf-8')
    req = urllib.request.Request(url.rstrip('/') + path, data=body,
        headers={'X-Token': token, 'Content-Type': 'application/json'})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=30) as response:
        return json.load(response)


def safe_name(name):
    return (isinstance(name, str) and bool(name) and not any(c in name for c in ('/', '\\', '..', ':', '\x00'))
            and not Path(name).is_absolute() and not PureWindowsPath(name).is_absolute())


def kill_tree(process):
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if process.poll() is None:
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def execute(job, workdir):
    result = dict(job_id=job['id'], exit_code=1, stdout='', stderr='', output_files={})
    try:
        with tempfile.TemporaryDirectory(prefix='job-', dir=workdir) as tmp:
            root = Path(tmp)
            inputs = job.get('files', {})
            for name, body in inputs.items():
                if not safe_name(name):
                    raise ValueError(f'Unsafe input filename: {name!r}')
                (root / name).write_bytes(base64.b64decode(body, validate=True))
            process = subprocess.Popen(job['command'], shell=True, cwd=tmp,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors='replace',
                start_new_session=(os.name != 'nt'))
            try:
                stdout, stderr = process.communicate(timeout=job['timeout_sec'])
                result['exit_code'] = process.returncode
            except subprocess.TimeoutExpired:
                kill_tree(process)
                stdout, stderr = process.communicate()
                result['exit_code'] = 124
                stderr += f'\nJob timed out after {job["timeout_sec"]} seconds\n'
            except BaseException:
                kill_tree(process)
                process.communicate()
                raise
            result['stdout'], result['stderr'] = stdout, stderr
            for directory, dirs, names in os.walk(root, followlinks=False):
                dirs[:] = [d for d in dirs if not (Path(directory) / d).is_symlink()]
                for name in names:
                    path = Path(directory) / name
                    relative = path.relative_to(root).as_posix()
                    if relative in inputs or path.is_symlink() or not path.is_file():
                        continue
                    if path.stat().st_size > MAX_FILE:
                        log(f'Skipping output over 50 MB: {relative}')
                        continue
                    with path.open('rb') as f:
                        body = f.read(MAX_FILE + 1)
                    if len(body) <= MAX_FILE:
                        result['output_files'][relative] = base64.b64encode(body).decode('ascii')
    except Exception as exc:
        result['exit_code'] = 1
        result['stderr'] += f'\nWorker error: {exc}\n'
    return result


def trim_result(result, limit=100 * 1024 * 1024):
    """Bound the actual JSON encoding, removing the largest attachments first."""
    files = result['output_files']
    for name in sorted(files, key=lambda key: len(files[key]), reverse=True):
        if len(json.dumps(result).encode('utf-8')) <= limit:
            break
        del files[name]
        result['stderr'] += f'\nDropped output file to fit result payload: {name}\n'


def report_result(call, result):
    trim_result(result)
    try:
        call('/result', result)
    except urllib.error.HTTPError as exc:
        if not 400 <= exc.code < 500:
            raise
        result['output_files'] = {}
        result['stderr'] += f'\nResult rejected with HTTP {exc.code}; retrying without output files.\n'
        try:
            call('/result', result)
        except (OSError, ValueError) as retry_error:
            log(f'Giving up result for job {result["job_id"]}: {retry_error}')
            return
    log(f'Reported job {result["job_id"]}')


def parse_labels(specs, parser):
    labels = {}
    for spec in specs:
        key, sep, value = spec.partition('=')
        if not sep or not key:
            parser.error(f'--label must be KEY=VALUE, got: {spec}')
        if key in labels:
            parser.error(f'duplicate --label key: {key}')
        labels[key] = value
    return labels


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--coordinator', required=True)
    parser.add_argument('--token', default=os.environ.get('SHARECOMPUTE_TOKEN'))
    parser.add_argument('--name', default=socket.gethostname())
    parser.add_argument('--label', action='append', default=[], metavar='KEY=VALUE',
                        help='label announced to the coordinator; repeatable')
    parser.add_argument('--poll-interval', type=float, default=5)
    parser.add_argument('--workdir', default='./sc-work')
    args = parser.parse_args()
    if not args.token:
        parser.error('--token or SHARECOMPUTE_TOKEN is required')
    if args.poll_interval <= 0:
        parser.error('--poll-interval must be positive')
    labels = parse_labels(args.label, parser)
    workdir = Path(args.workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    identity_lock = threading.Lock()
    identity = [None]
    def call(path, data):
        return request(args.coordinator, args.token, path, data)
    def heartbeat():
        delay = 20
        backoff = 5
        while not stop.wait(delay):
            with identity_lock:
                worker_id = identity[0]
            if worker_id is None:
                continue
            payload = {'worker_id': worker_id}
            if labels:
                payload['labels'] = labels
            try:
                call('/heartbeat', payload)
                delay, backoff = 20, 5
            except (OSError, ValueError) as exc:
                log(f'Heartbeat error: {exc}; retry in {backoff}s')
                delay, backoff = backoff, min(backoff * 2, 60)
    threading.Thread(target=heartbeat, daemon=True).start()
    worker_id = None
    pending_result = None
    backoff = 5
    try:
        while True:
            try:
                if pending_result is not None:
                    report_result(call, pending_result)
                    pending_result = None
                if worker_id is None:
                    registration = {'name': args.name, 'platform': platform.platform()}
                    if labels:
                        registration['labels'] = labels
                    worker_id = call('/register', registration)['worker_id']
                    with identity_lock:
                        identity[0] = worker_id
                    log(f'Registered as {worker_id}')
                try:
                    payload = {'worker_id': worker_id}
                    if labels:
                        payload['labels'] = labels
                    job = call('/poll', payload)['job']
                except urllib.error.HTTPError as exc:
                    if exc.code == 404:
                        worker_id = None  # Coordinator restarted; its registry is in memory.
                        continue
                    raise
                backoff = 5
                if job is None:
                    time.sleep(args.poll_interval)
                    continue
                log(f'Running job {job["id"]}')
                pending_result = execute(job, workdir)
                pending_result['worker_id'] = worker_id
            except (OSError, ValueError) as exc:
                log(f'Network/protocol error: {exc}; retry in {backoff}s')
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
    except KeyboardInterrupt:
        log('Stopping worker')
    finally:
        stop.set()


if __name__ == '__main__':
    main()
