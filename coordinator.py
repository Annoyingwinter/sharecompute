#!/usr/bin/env python3
"""A persistent, shared-secret authenticated task coordinator (stdlib only)."""
import base64
import binascii
import hmac
import json
import math
import os
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

MAX_BODY = 120 * 1024 * 1024
LOG_LIMIT = 200 * 1024


def valid_files(files, outputs=False):
    if not isinstance(files, dict):
        raise ValueError('files must be an object')
    for name, body in files.items():
        if not isinstance(name, str) or not name or any(c in name for c in ('\\', '..', ':', '\x00')) or name.startswith('/') or (not outputs and '/' in name):
            raise ValueError('file names must be simple basenames without ..')
        if not isinstance(body, str):
            raise ValueError('file bodies must be base64 strings')
        try:
            base64.b64decode(body, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError('invalid base64 file body') from exc
    return files


def valid_str_map(value, field):
    """Labels and requirements are optional objects mapping nonempty keys to string values."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(field + ' must be an object')
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise ValueError(field + ' keys must be nonempty strings')
        if not isinstance(item, str):
            raise ValueError(field + ' values must be strings')
    return value


def worker_satisfies(requires, worker):
    """True when every KEY=VALUE requirement matches; the key 'name' targets the worker name."""
    for key, value in (requires or {}).items():
        if key == 'name':
            if worker.get('name') != value:
                return False
        elif worker.get('labels', {}).get(key) != value:
            return False
    return True


class State:
    def __init__(self, path='jobs.json'):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.workers = {}
        self.jobs = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {}

    def save(self):
        """Caller holds the state lock; replacement never exposes partial JSON."""
        fd, name = tempfile.mkstemp(prefix='.jobs-', suffix='.tmp', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(self.jobs, f, ensure_ascii=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def reap(self):
        with self.lock:
            now = time.time()
            changed = False
            for job in self.jobs.values():
                if job['status'] == 'assigned' and now > job['lease_deadline']:
                    job['last_error'] = 'lease_expired'
                    job['lease_deadline'] = None
                    if job['attempts'] >= 3:
                        job.update(status='failed', finished_at=now, exit_code=1)
                    else:
                        job.update(status='pending', assigned_to=None, assigned_at=None)
                    changed = True
            if changed:
                self.save()


class APIError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, data):
        self.reply_body(code, json.dumps(data).encode('utf-8'), 'application/json')

    def reply_body(self, code, body, content_type):
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.handle_api()

    def do_POST(self):
        self.handle_api()

    def handle_api(self):
        try:
            path = urlsplit(self.path).path
            if self.command == 'GET' and path == '/':
                try:
                    body = Path(__file__).with_name('dashboard.html').read_bytes()
                except OSError:
                    self.reply_body(500, b'dashboard.html is missing or unreadable', 'text/plain; charset=utf-8')
                else:
                    self.reply_body(200, body, 'text/html; charset=utf-8')
                return
            if self.command == 'GET' and path == '/health':
                self.reply(200, {'ok': True})
                return
            token = self.headers.get('X-Token', '').encode('utf-8')
            if not hmac.compare_digest(token, self.server.token):
                raise APIError(401, 'invalid X-Token')
            data = {}
            if self.headers.get('Transfer-Encoding'):
                raise APIError(400, 'transfer encoding is unsupported; use Content-Length')
            length = int(self.headers.get('Content-Length', '0'))
            if length > MAX_BODY:
                raise APIError(413, 'payload_too_large')
            if length < 0:
                raise ValueError('invalid Content-Length')
            if self.command == 'POST':
                self.connection.settimeout(30)
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError('incomplete request body')
                data = json.loads(raw or b'{}')
                if not isinstance(data, dict):
                    raise ValueError('JSON body must be an object')
            with self.server.state.lock:
                response = self.route(path, data)
                # Serialize while locked, so another request cannot mutate the response.
                self.reply(200, response)
        except APIError as exc:
            self.reply(exc.code, {'error': exc.message})
        except (ValueError, TypeError, KeyError, UnicodeError) as exc:
            self.reply(400, {'error': str(exc)})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except OSError as exc:
            self.log_error('%s', exc)
            self.reply(500, {'error': 'storage or connection failure'})

    @staticmethod
    def job_summary(job):
        keys = ('id', 'command', 'status', 'assigned_to', 'attempts', 'created_at',
                'assigned_at', 'finished_at', 'exit_code')
        return {key: job[key] for key in keys}

    def route(self, path, data):
        state = self.server.state
        now = time.time()
        if self.command == 'GET':
            if path == '/workers':
                assigned = {j['assigned_to']: j['id'] for j in state.jobs.values() if j['status'] == 'assigned'}
                workers = [dict(id=wid, **w, age_sec=max(0, now - w['last_seen']),
                                online=now - w['last_seen'] <= 45, current_job=assigned.get(wid))
                           for wid, w in state.workers.items()]
                return sorted(workers, key=lambda w: (not w['online'], w['name']))
            if path == '/jobs':
                return [self.job_summary(j) for j in state.jobs.values()]
            if path.startswith('/jobs/'):
                job = state.jobs.get(path.removeprefix('/jobs/'))
                if job is not None:
                    return job
            raise APIError(404, 'not found')
        if path.startswith('/jobs/') and path.endswith('/cancel'):
            job = state.jobs.get(path[len('/jobs/'):-len('/cancel')])
            if job is None:
                raise APIError(404, 'unknown job')
            if job['status'] != 'pending':
                raise APIError(409, 'not_cancellable')
            job.update(status='cancelled', finished_at=now)
            state.save()
            return self.job_summary(job)
        if path == '/register':
            if not all(isinstance(data.get(k), str) and data[k] for k in ('name', 'platform')):
                raise ValueError('name and platform are required strings')
            worker_id = uuid.uuid4().hex
            state.workers[worker_id] = dict(name=data['name'], platform=data['platform'],
                                            labels=valid_str_map(data.get('labels'), 'labels'), last_seen=now)
            return {'worker_id': worker_id}
        if path == '/jobs':
            command = data.get('command')
            if not isinstance(command, str) or not command.strip():
                raise ValueError('command must be a nonempty string')
            timeout = data.get('timeout_sec', 3600)
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
                raise ValueError('timeout_sec must be finite and positive')
            files = valid_files(data.get('files', {}))
            requires = valid_str_map(data.get('requires'), 'requires')
            job_id = uuid.uuid4().hex
            state.jobs[job_id] = dict(id=job_id, command=command, files=files, timeout_sec=timeout,
                requires=requires,
                status='pending', assigned_to=None, attempts=0, created_at=now, assigned_at=None,
                finished_at=None, exit_code=None, stdout='', stderr='', output_files={}, last_error=None,
                lease_deadline=None)
            state.save()
            return {'job_id': job_id}
        if path not in ('/heartbeat', '/poll', '/result'):
            raise APIError(404, 'not found')
        worker_id = data.get('worker_id')
        if not isinstance(worker_id, str):
            raise ValueError('worker_id is required')
        # After a coordinator restart an assigned worker may still return its result.
        if path != '/result' and worker_id not in state.workers:
            raise APIError(404, 'unknown worker')
        if worker_id in state.workers:
            state.workers[worker_id]['last_seen'] = now
            if 'labels' in data:
                state.workers[worker_id]['labels'] = valid_str_map(data['labels'], 'labels')
        if path == '/heartbeat':
            return {'ok': True}
        if path == '/poll':
            worker = state.workers[worker_id]
            pending = [j for j in state.jobs.values()
                       if j['status'] == 'pending' and worker_satisfies(j.get('requires'), worker)]
            if not pending:
                return {'job': None}
            job = min(pending, key=lambda j: j['created_at'])
            job.update(status='assigned', assigned_to=worker_id, assigned_at=now,
                       lease_deadline=now + job['timeout_sec'] + 120, attempts=job['attempts'] + 1)
            state.save()
            return {'job': {k: job[k] for k in ('id', 'command', 'files', 'timeout_sec')}}
        job = state.jobs.get(data.get('job_id'))
        if job is None:
            raise APIError(404, 'unknown job')
        if job['assigned_to'] != worker_id:
            raise APIError(409, 'job belongs to another worker')
        # Idempotent retry if the previous response was lost in transit.
        if job['status'] in ('done', 'failed') and job['last_error'] != 'lease_expired':
            return {'ok': True}
        if job['status'] != 'assigned':
            raise APIError(409, 'job is not assigned')
        code = data.get('exit_code')
        if type(code) is not int:
            raise ValueError('exit_code must be an integer')
        logs = {}
        for key in ('stdout', 'stderr'):
            value = data.get(key, '')
            if not isinstance(value, str):
                raise ValueError(key + ' must be a string')
            logs[key] = value.encode('utf-8')[-LOG_LIMIT:].decode('utf-8', errors='ignore')
        files = valid_files(data.get('output_files', {}), outputs=True)
        job.update(status='done' if code == 0 else 'failed', exit_code=code, **logs,
                   output_files=files, finished_at=now, lease_deadline=None, last_error=None)
        state.save()
        return {'ok': True}


def main():
    token = os.environ.get('SHARECOMPUTE_TOKEN')
    if not token:
        raise SystemExit('Error: SHARECOMPUTE_TOKEN must be set to a nonempty shared secret')
    state = State()
    server = ThreadingHTTPServer(('0.0.0.0', int(os.environ.get('SC_PORT', '8765'))), Handler)
    server.state, server.token = state, token.encode('utf-8')
    stop = threading.Event()
    def reaper():
        while not stop.wait(15):
            try:
                state.reap()
            except OSError as exc:
                print(f'Lease reaper storage error: {exc}', flush=True)
    threading.Thread(target=reaper, daemon=True).start()
    print(f'Coordinator listening on 0.0.0.0:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()


if __name__ == '__main__':
    main()
