#!/usr/bin/env bash
set -uo pipefail
REPO=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PYTHON=${PYTHON:-python3}
TMP=$(mktemp -d)
RESULT="$REPO/SMOKE_RESULT.txt"
COORD_PID= WORKER_PID=
FAILED=0
: > "$RESULT"
check() {
    local name=$1
    shift
    if "$@"; then printf 'PASS: %s\n' "$name" >> "$RESULT"
    else printf 'FAIL: %s\n' "$name" >> "$RESULT"; FAILED=1; fi
}
cleanup() {
    trap - EXIT
    for pid in "$WORKER_PID" "$COORD_PID"; do
        if [[ -n "$pid" ]]; then kill "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true; fi
    done
    if [[ "$FAILED" == 0 ]]; then echo 'SMOKE: PASS' >> "$RESULT"
    else echo 'SMOKE: FAIL' >> "$RESULT"; cat "$TMP"/*.log 2>/dev/null || true; fi
    cat "$RESULT"
    rm -rf -- "$TMP"
}
trap cleanup EXIT
trap 'FAILED=1; exit 1' INT TERM
export SC_PORT=18765 SHARECOMPUTE_TOKEN=smoke-test-token
export NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost
URL=http://127.0.0.1:18765
(cd "$TMP" && exec "$PYTHON" "$REPO/coordinator.py") > "$TMP/coordinator.log" 2>&1 &
COORD_PID=$!
READY=0
for ((i=0; i<50; i++)); do
    if ! kill -0 "$COORD_PID" 2>/dev/null; then break; fi
    if "$PYTHON" -c 'import json,urllib.request; assert json.load(urllib.request.urlopen("http://127.0.0.1:18765/health", timeout=1))["ok"]' 2>/dev/null; then READY=1; break; fi
    sleep 0.1
done
check 'coordinator healthy' test "$READY" = 1
if [[ "$READY" != 1 ]]; then exit 1; fi
"$PYTHON" "$REPO/worker.py" --coordinator "$URL" --name smoke-worker --label gpu=smoke --poll-interval 0.1 --workdir "$TMP/work" > "$TMP/worker.log" 2>&1 &
WORKER_PID=$!
check 'dashboard is public and contains ShareCompute' "$PYTHON" - "$URL" <<'PYTEST'
import sys, urllib.request
with urllib.request.urlopen(sys.argv[1] + '/', timeout=3) as response:
    assert response.status == 200
    assert b'ShareCompute' in response.read()
PYTEST
check 'workers lists online smoke worker' "$PYTHON" - "$URL" <<'PYTEST'
import json, os, sys, time, urllib.request
for attempt in range(50):
    request = urllib.request.Request(sys.argv[1] + '/workers', headers={'X-Token': os.environ['SHARECOMPUTE_TOKEN']})
    with urllib.request.urlopen(request, timeout=3) as response:
        workers = json.load(response)
    if any(w['name'] == 'smoke-worker' and w['online'] is True for w in workers):
        break
    time.sleep(.1)
else:
    raise AssertionError('smoke worker did not register')
PYTEST
check 'workers include labels gpu=smoke' "$PYTHON" - "$URL" <<'PYTEST'
import json, os, sys, time, urllib.request
for attempt in range(50):
    request = urllib.request.Request(sys.argv[1] + '/workers', headers={'X-Token': os.environ['SHARECOMPUTE_TOKEN']})
    with urllib.request.urlopen(request, timeout=3) as response:
        workers = json.load(response)
    smoke = [w for w in workers if w['name'] == 'smoke-worker']
    if smoke and smoke[0].get('labels') == {'gpu': 'smoke'}:
        break
    time.sleep(.1)
else:
    raise AssertionError('smoke worker did not report labels')
PYTEST
check 'job A completes; job B cancels and never executes' "$PYTHON" - "$URL" "$WORKER_PID" "$TMP" <<'PYTEST'
import json, os, shlex, signal, sys, time, urllib.request
from pathlib import Path
url, pid, folder = sys.argv[1], int(sys.argv[2]), Path(sys.argv[3])
def api(path, data=None):
    request = urllib.request.Request(url + path, data=None if data is None else json.dumps(data).encode(),
        headers={'X-Token': os.environ['SHARECOMPUTE_TOKEN'], 'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=3) as response:
        return json.load(response)
def wait(jid):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        job = api('/jobs/' + jid)
        if job['status'] in ('done', 'failed', 'cancelled'):
            assert job['status'] == 'done' and job['exit_code'] == 0, job
            return
        time.sleep(.1)
    raise AssertionError('job did not finish')
a = api('/jobs', {'command': 'echo dashboard-job-A'})['job_id']
wait(a)
marker = folder / 'cancelled-job-ran.txt'
# Stop only this smoke worker between jobs to make pending cancellation deterministic.
os.kill(pid, signal.SIGSTOP)
try:
    command = 'echo should-not-run > ' + shlex.quote(str(marker))
    b = api('/jobs', {'command': command})['job_id']
    assert api('/jobs/' + b + '/cancel', {})['status'] == 'cancelled'
    assert api('/jobs/' + b)['status'] == 'cancelled'
finally:
    os.kill(pid, signal.SIGCONT)
# A subsequent completed job proves the resumed worker polled past job B.
wait(api('/jobs', {'command': 'echo after-cancellation'})['job_id'])
assert api('/jobs/' + b)['status'] == 'cancelled'
assert not marker.exists(), 'cancelled job executed'
PYTEST
timeout 45 "$PYTHON" "$REPO/submit.py" --coordinator "$URL" --wait --outdir "$TMP/out1" -- 'echo hello-farm && echo farm-output-content > out.txt' > "$TMP/job1.log" 2>&1
check 'job 1 exit code is 0' test "$?" = 0
check 'job 1 output contents' grep -q 'farm-output-content' "$TMP/out1/out.txt"
printf 'roundtrip-42\n' > "$TMP/in.txt"
# Python is present on every worker; quote its executable for the Linux smoke shell.
COPY_COMMAND=$("$PYTHON" -c 'import shlex,sys; print(shlex.quote(sys.executable)+" -c "+shlex.quote("import shutil; shutil.copyfile(\"in.txt\", \"copied.txt\")"))')
timeout 45 "$PYTHON" "$REPO/submit.py" --coordinator "$URL" --file "$TMP/in.txt" --wait --outdir "$TMP/out2" -- "$COPY_COMMAND" > "$TMP/job2.log" 2>&1
check 'job 2 exit code is 0' test "$?" = 0
check 'job 2 roundtrip contents' grep -q 'roundtrip-42' "$TMP/out2/copied.txt"
timeout 45 "$PYTHON" "$REPO/submit.py" --coordinator "$URL" --require gpu=smoke --wait --outdir "$TMP/out3" -- 'echo targeted-job-c' > "$TMP/job3.log" 2>&1
check 'job C with --require gpu=smoke completes' test "$?" = 0
check 'job C stdout captured' grep -q 'targeted-job-c' "$TMP/job3.log"
JOB_D=$(timeout 20 "$PYTHON" "$REPO/submit.py" --coordinator "$URL" --require gpu=nonexistent -- 'echo job-d-ran' 2> "$TMP/jobd.err")
check 'job D submits with unmatched requirement' test -n "$JOB_D"
check 'job D stays pending after 10 s then cancels' "$PYTHON" - "$URL" "$JOB_D" <<'PYTEST'
import json, os, sys, time, urllib.request
url, jid = sys.argv[1], sys.argv[2]
def api(path, data=None):
    request = urllib.request.Request(url + path, data=None if data is None else json.dumps(data).encode(),
        headers={'X-Token': os.environ['SHARECOMPUTE_TOKEN'], 'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=3) as response:
        return json.load(response)
time.sleep(10)
job = api('/jobs/' + jid)
assert job['status'] == 'pending', f'job D should still be pending, got {job["status"]}'
assert job['assigned_to'] is None and job['attempts'] == 0, job
assert api('/jobs/' + jid + '/cancel', {})['status'] == 'cancelled'
PYTEST
mkdir -p "$TMP/eval-data"
for i in 1 2 3 4; do printf 'data-%s' "$i" > "$TMP/eval-data/f$i.txt"; done
timeout 45 "$PYTHON" "$REPO/scatter_eval.py" --coordinator "$URL" --dir "$TMP/eval-data" --shard-size 2 --outdir "$TMP/scatter-out" --wait-timeout 30 --merge-cmd 'cp "$SC_SUMMARY" merged-summary.json' --cmd 'mkdir -p {out}; for f in {files}; do cp "$f" "{out}/$f.done"; done' > "$TMP/scatter.log" 2>&1
check 'scatter exit code is 0' test "$?" = 0
check 'scatter reports 2/2 done' grep -q '^SCATTER: 2/2 shards done$' "$TMP/scatter.log"
check 'scatter merge hook reports exit 0' grep -q '^MERGE: exit 0$' "$TMP/scatter.log"
check 'scatter merged summary exists next to summary' test -f "$TMP/scatter-out/merged-summary.json"
check 'scatter merged summary matches summary' cmp -s "$TMP/scatter-out/summary.json" "$TMP/scatter-out/merged-summary.json"
check 'scatter outputs and summary' "$PYTHON" - "$TMP/scatter-out" <<'PYTEST'
import json, sys
from pathlib import Path
root = Path(sys.argv[1])
for i in range(1, 5):
    assert (root / f'shard-{(i-1)//2}' / 'results' / f'f{i}.txt.done').read_text() == f'data-{i}'
summary = json.loads((root / 'summary.json').read_text())
assert len(summary) == 2
assert all(s['status'] == 'done' and s['exit_code'] == 0 for s in summary)
assert [s['files'] for s in summary] == [['f1.txt', 'f2.txt'], ['f3.txt', 'f4.txt']]
PYTEST
exit "$FAILED"
