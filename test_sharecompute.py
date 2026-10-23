"""Focused stdlib regression checks; run with python3 -m unittest -v."""
import base64
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer

import coordinator
import submit
import worker


class APITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = coordinator.State(Path(self.tmp.name) / 'jobs.json')
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), coordinator.Handler)
        self.server.state, self.server.token = self.state, b'test-token'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def call(self, path, data=None, token='test-token', headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        try:
            body = None if data is None else json.dumps(data)
            hdr = {'X-Token': token, 'Content-Type': 'application/json'}
            hdr.update(headers or {})
            conn.request('GET' if data is None else 'POST', path, body, hdr)
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def register(self):
        code, response = self.call('/register', {'name': 'test', 'platform': 'Linux'})
        self.assertEqual(code, 200)
        return response['worker_id']

    def create(self):
        code, response = self.call('/jobs', {'command': 'echo test'})
        self.assertEqual(code, 200)
        return response['job_id']

    def test_dashboard_is_public(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port)
        self.addCleanup(conn.close)
        conn.request('GET', '/')
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader('Content-Type'), 'text/html; charset=utf-8')
        self.assertIn(b'ShareCompute', response.read())

    def test_missing_dashboard_returns_plain_text_error(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port)
        self.addCleanup(conn.close)
        with patch.object(Path, 'read_bytes', side_effect=FileNotFoundError):
            conn.request('GET', '/')
            response = conn.getresponse()
            self.assertEqual(response.status, 500)
            self.assertEqual(response.getheader('Content-Type'), 'text/plain; charset=utf-8')
            self.assertIn(b'dashboard.html', response.read())

    def test_workers_presence_sort_and_assignment(self):
        self.assertEqual(self.call('/workers', token='')[0], 401)
        offline = self.register()
        online = self.call('/register', {'name': 'Zebra', 'platform': 'Linux'})[1]['worker_id']
        other = self.call('/register', {'name': 'Alpha', 'platform': 'Windows'})[1]['worker_id']
        with self.state.lock:
            self.state.workers[offline]['last_seen'] -= 60
        jid = self.create()
        self.call('/poll', {'worker_id': online})
        code, workers = self.call('/workers')
        self.assertEqual(code, 200)
        self.assertEqual([w['id'] for w in workers], [other, online, offline])
        self.assertEqual(workers[1]['current_job'], jid)
        self.assertIsNone(workers[0]['current_job'])
        self.assertTrue(workers[1]['online'])
        self.assertFalse(workers[2]['online'])
        self.assertGreaterEqual(workers[2]['age_sec'], 60)

    def test_cancel_persistence_poll_and_terminal_wait(self):
        import sc_client
        jid = self.create()
        path = '/jobs/' + jid + '/cancel'
        self.assertEqual(self.call(path, {}, token='')[0], 401)
        code, job = self.call(path, {})
        self.assertEqual(code, 200)
        self.assertEqual(job['status'], 'cancelled')
        self.assertIsNotNone(job['finished_at'])
        self.assertEqual(self.call('/jobs/' + jid)[1]['status'], 'cancelled')
        self.assertEqual(coordinator.State(self.state.path).jobs[jid]['status'], 'cancelled')
        self.assertEqual(self.call(path, {}), (409, {'error': 'not_cancellable'}))
        self.assertEqual(self.call('/jobs/missing/cancel', {})[0], 404)
        wid = self.register()
        self.assertIsNone(self.call('/poll', {'worker_id': wid})[1]['job'])
        active = self.create()
        self.call('/poll', {'worker_id': wid})
        self.assertEqual(self.call('/jobs/' + active + '/cancel', {})[0], 409)
        self.call('/result', dict(worker_id=wid, job_id=active, exit_code=0))
        self.assertEqual(self.call('/jobs/' + active + '/cancel', {})[0], 409)
        url = f'http://127.0.0.1:{self.server.server_port}'
        self.assertEqual(sc_client.wait_job(url, 'test-token', jid, wait_timeout=.1)['status'], 'cancelled')
        self.assertEqual(self.call('/jobs')[1][0]['command'], 'echo test')

    def test_auth_validation_and_size_limit(self):
        self.assertEqual(self.call('/health', token='')[0], 200)
        self.assertEqual(self.call('/jobs', token='')[0], 401)
        self.assertEqual(self.call('/register', {}, token='wrong')[0], 401)
        self.assertEqual(self.call('/jobs', {}, headers={'Content-Length': str(120 * 1024 * 1024 + 1)}), (413, {'error': 'payload_too_large'}))
        for timeout in (0, -1, True, '10', float('inf')):
            self.assertEqual(self.call('/jobs', {'command': 'true', 'timeout_sec': timeout})[0], 400)
        for name in ('../evil', '/tmp/evil', 'a\\b', 'a..b'):
            self.assertEqual(self.call('/jobs', {'command': 'true', 'files': {name: ''}})[0], 400)

    def test_atomic_poll_result_owner_and_persistence(self):
        first, second = self.create(), self.create()
        ids = [self.register() for _ in range(6)]
        with ThreadPoolExecutor(max_workers=6) as pool:
            replies = list(pool.map(lambda wid: self.call('/poll', {'worker_id': wid}), ids))
        assigned = [(wid, response['job']['id']) for wid, (code, response) in zip(ids, replies) if response['job']]
        self.assertEqual({jid for _, jid in assigned}, {first, second})
        self.assertEqual(len(assigned), 2)
        owner, jid = assigned[0]
        wrong = next(wid for wid in ids if wid != owner)
        result = dict(worker_id=wrong, job_id=jid, exit_code=0, stdout='界' * 100000,
                      stderr='x' * 250000, output_files={'nested/out.txt': 'b2s='})
        self.assertEqual(self.call('/result', result)[0], 409)
        result['worker_id'] = owner
        self.assertEqual(self.call('/result', result)[0], 200)
        self.assertEqual(self.call('/result', result)[0], 200)
        record = self.call('/jobs/' + jid)[1]
        self.assertEqual(record['status'], 'done')
        self.assertLessEqual(len(record['stdout'].encode()), 204800)
        self.assertEqual(len(record['stderr']), 204800)
        self.assertEqual(record['output_files'], {'nested/out.txt': 'b2s='})
        self.assertNotIn('files', self.call('/jobs')[1][0])
        restored = coordinator.State(self.state.path)
        self.assertEqual(restored.jobs[jid], record)

    def test_oldest_job_lease_retry_and_terminal_failure(self):
        first, second = self.create(), self.create()
        wid = self.register()
        for attempt in range(1, 4):
            response = self.call('/poll', {'worker_id': wid})[1]
            self.assertEqual(response['job']['id'], first)
            with self.state.lock:
                self.assertEqual(self.state.jobs[first]['attempts'], attempt)
                self.state.jobs[first]['lease_deadline'] = time.time() - 1
            self.state.reap()
            expected = 'pending' if attempt < 3 else 'failed'
            self.assertEqual(self.call('/jobs/' + first)[1]['status'], expected)
        record = self.call('/jobs/' + first)[1]
        self.assertEqual(record['last_error'], 'lease_expired')
        self.assertIsNotNone(record['finished_at'])
        self.assertEqual(self.call('/poll', {'worker_id': wid})[1]['job']['id'], second)

    def test_submit_wait_timeout(self):
        result = subprocess.run([sys.executable, str(Path(submit.__file__).resolve()),
            '--coordinator', f'http://127.0.0.1:{self.server.server_port}',
            '--token', 'test-token', '--wait', '--wait-timeout', '0.1', '--', 'echo test'],
            capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 124)
        self.assertIn('timed out', result.stderr)

    def test_scatter_missing_input_submits_nothing(self):
        folder = Path(self.tmp.name) / 'inputs'
        folder.mkdir()
        (folder / 'a.txt').write_text('valid')
        (folder / 'z.txt').symlink_to(folder / 'missing.txt')
        result = subprocess.run([sys.executable, str(Path(__file__).with_name('scatter_eval.py')),
            '--coordinator', f'http://127.0.0.1:{self.server.server_port}', '--token', 'test-token',
            '--dir', str(folder), '--cmd', 'echo {files}'], capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 1)
        self.assertIn('z.txt', result.stderr)
        self.assertEqual(self.state.jobs, {})

    def test_scatter_overall_timeout_writes_summary(self):
        folder = Path(self.tmp.name) / 'data'
        folder.mkdir()
        for name in ('a.txt', 'b.txt'):
            (folder / name).write_text('input')
        output = Path(self.tmp.name) / 'out'
        result = subprocess.run([sys.executable, str(Path(__file__).with_name('scatter_eval.py')),
            '--coordinator', f'http://127.0.0.1:{self.server.server_port}', '--token', 'test-token',
            '--dir', str(folder), '--outdir', str(output), '--wait-timeout', '0.1',
            '--cmd', 'echo {files}'], capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 1)
        self.assertIn('SCATTER: 0/2 shards done', result.stdout)
        summary = json.loads((output / 'summary.json').read_text())
        self.assertEqual([r['status'] for r in summary], ['wait_timeout', 'wait_timeout'])
        self.assertEqual(len(self.state.jobs), 2)

    def test_shared_client_submission_and_terminal_wait(self):
        import sc_client
        url = f'http://127.0.0.1:{self.server.server_port}'
        jid = sc_client.submit_job(url, 'test-token', 'exit 7', {}, 10)
        self.assertEqual(sc_client.get_job(url, 'test-token', jid)['status'], 'pending')
        wid = self.register()
        self.call('/poll', {'worker_id': wid})
        self.call('/result', dict(worker_id=wid, job_id=jid, exit_code=7))
        job = sc_client.wait_job(url, 'test-token', jid, wait_timeout=0.5)
        self.assertEqual((job['status'], job['exit_code']), ('failed', 7))

    def test_missing_token_stops_startup(self):
        env = dict(os.environ)
        env.pop('SHARECOMPUTE_TOKEN', None)
        result = subprocess.run([sys.executable, str(Path(coordinator.__file__).resolve())],
            env=env, capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('SHARECOMPUTE_TOKEN', result.stderr)

    def test_labels_targeted_poll_and_refresh(self):
        labeled = self.call('/register', {'name': 'gpu-box', 'platform': 'Linux',
                                          'labels': {'gpu': 'smoke'}})[1]['worker_id']
        plain = self.register()  # name 'test', no labels
        entry = next(w for w in self.call('/workers')[1] if w['id'] == labeled)
        self.assertEqual(entry['labels'], {'gpu': 'smoke'})
        self.assertEqual(next(w for w in self.call('/workers')[1] if w['id'] == plain)['labels'], {})
        # Older unmatched job must not block a newer matching one for the labeled worker.
        unmatched = self.call('/jobs', {'command': 'echo x', 'requires': {'gpu': 'nonexistent'}})[1]['job_id']
        targeted = self.call('/jobs', {'command': 'echo x', 'requires': {'gpu': 'smoke'}})[1]['job_id']
        by_name = self.call('/jobs', {'command': 'echo x', 'requires': {'name': 'gpu-box'}})[1]['job_id']
        self.assertIsNone(self.call('/poll', {'worker_id': plain})[1]['job'])
        self.assertEqual(self.call('/jobs/' + unmatched)[1]['status'], 'pending')
        job = self.call('/poll', {'worker_id': labeled})[1]['job']
        self.assertEqual(job['id'], targeted)
        job = self.call('/poll', {'worker_id': labeled})[1]['job']
        self.assertEqual(job['id'], by_name)
        self.assertIsNone(self.call('/poll', {'worker_id': labeled})[1]['job'])
        self.assertEqual(self.call('/jobs/' + unmatched)[1]['requires'], {'gpu': 'nonexistent'})
        # Labels refresh on poll; the stale gpu label stops matching immediately.
        self.assertEqual(self.call('/poll', {'worker_id': labeled, 'labels': {'gpu': 'other'}})[1]['job'], None)
        entry = next(w for w in self.call('/workers')[1] if w['id'] == labeled)
        self.assertEqual(entry['labels'], {'gpu': 'other'})
        self.assertEqual(self.call('/heartbeat', {'worker_id': labeled, 'labels': {'gpu': 'back'}}), (200, {'ok': True}))
        entry = next(w for w in self.call('/workers')[1] if w['id'] == labeled)
        self.assertEqual(entry['labels'], {'gpu': 'back'})
        for bad in ({'gpu': 5}, {'gpu': None}, ['gpu=smoke']):
            self.assertEqual(self.call('/register', {'name': 'n', 'platform': 'p', 'labels': bad})[0], 400)
            self.assertEqual(self.call('/jobs', {'command': 'true', 'requires': bad})[0], 400)
            self.assertEqual(self.call('/poll', {'worker_id': labeled, 'labels': bad})[0], 400)

    def test_require_and_worker_flags_reach_job_record(self):
        url = f'http://127.0.0.1:{self.server.server_port}'
        result = subprocess.run([sys.executable, str(Path(submit.__file__).resolve()),
            '--coordinator', url, '--token', 'test-token',
            '--require', 'gpu=4060', '--require', 'ram=32G', '--', 'echo test'],
            capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.call('/jobs/' + result.stdout.strip())[1]['requires'], {'gpu': '4060', 'ram': '32G'})
        result = subprocess.run([sys.executable, str(Path(submit.__file__).resolve()),
            '--coordinator', url, '--token', 'test-token', '--worker', 'laptop-4060', '--', 'echo test'],
            capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.call('/jobs/' + result.stdout.strip())[1]['requires'], {'name': 'laptop-4060'})

    def test_scatter_merge_cmd_runs_regardless_of_shards(self):
        folder = Path(self.tmp.name) / 'data'
        folder.mkdir()
        (folder / 'a.txt').write_text('input')
        output = Path(self.tmp.name) / 'out'
        base = [sys.executable, str(Path(__file__).with_name('scatter_eval.py')),
            '--coordinator', f'http://127.0.0.1:{self.server.server_port}', '--token', 'test-token',
            '--dir', str(folder), '--outdir', str(output), '--wait-timeout', '0.1', '--cmd', 'echo {files}']
        result = subprocess.run(base + ['--merge-cmd', 'cp "$SC_SUMMARY" "$SC_OUTDIR/merged.json"'],
            capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1)  # Shards timed out, but the merge still ran.
        self.assertIn('SCATTER: 0/1 shards done', result.stdout)
        self.assertIn('MERGE: exit 0', result.stdout)
        self.assertEqual(json.loads((output / 'merged.json').read_text()),
                         json.loads((output / 'summary.json').read_text()))
        result = subprocess.run(base + ['--merge-cmd', 'exit 3'], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertIn('MERGE: exit 3', result.stdout)


class RequirementMatcherTests(unittest.TestCase):
    def worker(self, name='w1', labels=None):
        return {'name': name, 'labels': dict(labels or {})}

    def test_empty_requires_match_any_worker(self):
        self.assertTrue(coordinator.worker_satisfies({}, self.worker()))
        self.assertTrue(coordinator.worker_satisfies({}, self.worker(labels={'gpu': '4060'})))
        self.assertTrue(coordinator.worker_satisfies(None, self.worker()))
        self.assertTrue(coordinator.worker_satisfies({}, {'name': 'w1'}))

    def test_label_match(self):
        worker = self.worker(labels={'gpu': '4060', 'ram': '32G'})
        self.assertTrue(coordinator.worker_satisfies({'gpu': '4060'}, worker))
        self.assertTrue(coordinator.worker_satisfies({'gpu': '4060', 'ram': '32G'}, worker))
        self.assertTrue(coordinator.worker_satisfies({'gpu': ''}, self.worker(labels={'gpu': ''})))

    def test_label_mismatch(self):
        worker = self.worker(labels={'gpu': '4060'})
        self.assertFalse(coordinator.worker_satisfies({'gpu': '5090'}, worker))
        self.assertFalse(coordinator.worker_satisfies({'gpu': '4060', 'ram': '32G'}, worker))
        self.assertFalse(coordinator.worker_satisfies({'tpu': 'v4'}, worker))

    def test_name_match(self):
        worker = self.worker('laptop', {'gpu': '4060'})
        self.assertTrue(coordinator.worker_satisfies({'name': 'laptop'}, worker))
        self.assertFalse(coordinator.worker_satisfies({'name': 'cloud'}, worker))
        # The reserved key 'name' targets the worker name, never a label of that key.
        self.assertTrue(coordinator.worker_satisfies({'name': 'laptop'}, self.worker('laptop', {'name': 'cloud'})))
        self.assertFalse(coordinator.worker_satisfies({'name': 'cloud'}, self.worker('laptop', {'name': 'cloud'})))
        self.assertTrue(coordinator.worker_satisfies({'name': 'laptop', 'gpu': '4060'}, worker))
        self.assertFalse(coordinator.worker_satisfies({'name': 'laptop', 'gpu': '5090'}, worker))


class WorkerTests(unittest.TestCase):
    def test_result_budget_drops_largest_first(self):
        result = dict(job_id='j', worker_id='w', exit_code=0, stdout='', stderr='',
                      output_files={'large': 'a' * 2000, 'small': 'b' * 20})
        worker.trim_result(result, limit=400)
        self.assertEqual(result['output_files'], {'small': 'b' * 20})
        self.assertIn('large', result['stderr'])
        self.assertLessEqual(len(json.dumps(result).encode()), 400)

    def test_result_client_error_retries_once_then_moves_on(self):
        import urllib.error
        for code in (400, 401, 404, 409, 413):
            calls = []
            def reject(path, result):
                calls.append(dict(result, output_files=dict(result['output_files'])))
                raise urllib.error.HTTPError('http://test', code, 'rejected', {}, None)
            result = dict(job_id='j', stderr='', output_files={'out': 'YQ=='})
            worker.report_result(reject, result)
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[1]['output_files'], {})
            self.assertIn(str(code), calls[1]['stderr'])

    def test_input_rejection_cleanup_and_nested_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = worker.execute(dict(id='bad', command='echo unsafe', files={'../escape': ''}, timeout_sec=2), tmp)
            self.assertEqual(result['exit_code'], 1)
            self.assertIn('Unsafe input', result['stderr'])
            code = 'from pathlib import Path; Path("nested").mkdir(); Path("nested/out.txt").write_bytes(Path("in.txt").read_bytes())'
            command = subprocess.list2cmdline([sys.executable, '-c', code]) if os.name == 'nt' else shlex.join([sys.executable, '-c', code])
            result = worker.execute(dict(id='ok', command=command, files={'in.txt': 'aGVsbG8='}, timeout_sec=5), tmp)
            self.assertEqual(result['exit_code'], 0)
            self.assertEqual(result['output_files'], {'nested/out.txt': 'aGVsbG8='})
            self.assertEqual(list(Path(tmp).iterdir()), [])

    @unittest.skipIf(os.name == 'nt', 'POSIX process-group verification')
    def test_timeout_kills_descendant(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / 'escaped.txt'
            child = f'import time; from pathlib import Path; time.sleep(1); Path({str(marker)!r}).write_text("escaped")'
            parent = f'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",{child!r}]); time.sleep(10)'
            command = shlex.join([sys.executable, '-c', parent])
            result = worker.execute(dict(id='timeout', command=command, files={}, timeout_sec=0.2), tmp)
            self.assertEqual(result['exit_code'], 124)
            self.assertIn('timed out', result['stderr'])
            time.sleep(1.2)
            self.assertFalse(marker.exists(), 'descendant survived timeout and wrote a file')
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_output_cannot_escape_outdir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            for name in ('../escape', '/absolute', 'C:\\escape', 'nested/../../escape'):
                with self.assertRaises(ValueError):
                    submit.output_path(root, name)
            self.assertEqual(submit.output_path(root, 'nested/out.txt'), root / 'nested/out.txt')


if __name__ == '__main__':
    unittest.main()
