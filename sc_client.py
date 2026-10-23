"""Small stdlib HTTP client. File mappings contain base64-encoded strings."""
import json
import math
import time
import urllib.request


def _request(coord, token, path, data=None, timeout=30):
    body = None if data is None else json.dumps(data).encode('utf-8')
    req = urllib.request.Request(coord.rstrip('/') + path, data=body,
        headers={'X-Token': token, 'Content-Type': 'application/json'})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=timeout) as response:
        return json.load(response)


def parse_requires(specs):
    """Parse repeated KEY=VALUE CLI strings into a requirements dict."""
    requires = {}
    for spec in specs:
        key, sep, value = spec.partition('=')
        if not sep or not key:
            raise ValueError(f'requirements must be KEY=VALUE, got: {spec}')
        if key in requires:
            raise ValueError(f'duplicate requirement key: {key}')
        requires[key] = value
    return requires


def submit_job(coord, token, command, files, timeout_sec, requires=None):
    return _request(coord, token, '/jobs', dict(command=command, files=files,
                    timeout_sec=timeout_sec, requires=requires or {}))['job_id']


def get_job(coord, token, job_id):
    return _request(coord, token, '/jobs/' + job_id)


def wait_job(coord, token, job_id, poll=2, wait_timeout=0):
    if not math.isfinite(wait_timeout) or wait_timeout < 0:
        raise ValueError('wait_timeout must be finite and nonnegative')
    if not math.isfinite(poll) or poll <= 0:
        raise ValueError('poll must be finite and positive')
    deadline = time.monotonic() + wait_timeout if wait_timeout else None
    while True:
        remaining = deadline - time.monotonic() if deadline else None
        if remaining is not None and remaining <= 0:
            raise TimeoutError(f'Wait timed out for job {job_id} after {wait_timeout:g} seconds')
        try:
            job = _request(coord, token, '/jobs/' + job_id,
                           timeout=min(30, remaining) if remaining is not None else 30)
        except TimeoutError:
            if deadline and time.monotonic() >= deadline:
                raise TimeoutError(f'Wait timed out for job {job_id}') from None
            raise
        if job['status'] in ('done', 'failed', 'cancelled'):
            return job
        remaining = deadline - time.monotonic() if deadline else poll
        if remaining > 0:
            time.sleep(min(poll, remaining))
