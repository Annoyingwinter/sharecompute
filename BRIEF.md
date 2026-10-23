# ShareCompute — lightweight task farm (build brief)

Build a tiny task farm: a coordinator hands out shell jobs over HTTP to worker
machines (Windows/Linux) on a trusted private network (Tailscale). The owner
uses it to spread independent batch jobs (evals, conversions, sweeps) across
their own PCs.

## Hard constraints
- Python 3 standard library ONLY. No pip dependencies, no third-party imports.
- Python 3.10+.
- Write all deliverables into the current directory.

## Deliverables

### coordinator.py
- ThreadingHTTPServer, bind 0.0.0.0, port from env SC_PORT (default 8765).
- Auth: every endpoint except GET /health requires header `X-Token` equal to
  env SHARECOMPUTE_TOKEN, compared with hmac.compare_digest. If
  SHARECOMPUTE_TOKEN is unset, exit at startup with a clear error message.
- State: jobs persisted to jobs.json in the cwd, written atomically (temp file
  + os.replace). Loaded on startup if present.
- Job record fields: id (uuid4 hex), command (string, executed via shell),
  files {name: base64} (inputs), timeout_sec (default 3600), status
  (pending|assigned|done|failed), assigned_to, attempts, created_at,
  assigned_at, finished_at, exit_code, stdout, stderr,
  output_files {name: base64}, last_error.
- Endpoints (JSON request/response):
  - GET /health -> {"ok": true} (no auth)
  - POST /register {name, platform} -> {"worker_id": uuid}; keep a workers
    registry with last_seen timestamps.
  - POST /heartbeat {worker_id} -> update last_seen, return {"ok": true}.
  - POST /poll {worker_id} -> atomically take the oldest pending job: mark it
    assigned, assigned_at=now, lease deadline = now + timeout_sec + 120,
    attempts += 1; return {job: {id, command, files, timeout_sec}} or
    {"job": null}.
  - POST /result {worker_id, job_id, exit_code, stdout, stderr,
    output_files} -> accept only from the worker the job is assigned to;
    status = done if exit_code == 0 else failed; truncate stdout/stderr to
    the last 200 KB each.
  - POST /jobs {command, files?, timeout_sec?} -> create a pending job,
    return {"job_id"}.
  - GET /jobs -> list jobs without file bodies (id, status, assigned_to,
    attempts, timestamps, exit_code).
  - GET /jobs/{id} -> the full job record.
- Lease reaper: background thread, every 15 s: assigned jobs past their lease
  deadline go back to pending, unless attempts >= 3, in which case mark them
  failed with last_error="lease_expired".
- Reject request bodies larger than 120 MB with HTTP 413.
- Thread-safe: guard all shared state with one lock.

### worker.py
- Cross-platform (Windows, Linux, macOS), stdlib only.
- Args: --coordinator URL (required), --token (default: env
  SHARECOMPUTE_TOKEN), --name (default: hostname), --poll-interval
  (default 5), --workdir (default ./sc-work).
- Loop forever: register once; heartbeat every 20 s; poll for jobs.
- On a job: create a fresh temp dir under workdir; write input files
  (base64-decode; refuse file names containing path separators, "..", or
  absolute paths); run the command with shell=True, cwd=temp dir, capture
  stdout/stderr as text (errors="replace"); enforce timeout_sec — on timeout
  kill the whole process tree (POSIX: start the child in a new session and
  kill the process group; Windows: taskkill /T /F). Collect output files:
  every file in the temp dir that was not an input, each at most 50 MB,
  base64-encoded. POST /result. Delete the temp dir.
- Network errors: exponential backoff from 5 s up to 60 s; never crash out.
- Log lines with timestamps to stdout.

### submit.py
- Args: --coordinator (required), --token (default env SHARECOMPUTE_TOKEN),
  --timeout (job timeout seconds, default 3600), --file PATH or
  --file PATH=NAME (repeatable), --wait, --outdir DIR, and the job command
  given after `--` (or as remaining positional args).
- Submits the job, prints the job_id. With --wait: poll GET /jobs/{id} every
  2 s until done/failed; print stdout and stderr; write output files into
  --outdir (default ./sc-out); exit with the job's exit code.

### build_worker.bat
- Windows batch file: locate `py` or `python`, run PyInstaller
  (--onefile --name sharecompute-worker worker.py), print the path of the
  produced exe (dist\sharecompute-worker.exe). If PyInstaller is not
  installed, print `pip install pyinstaller` instructions and stop.

### smoke_test.sh
- Bash script for Linux. Uses port 18765 and token smoke-test-token, a temp
  working directory. Starts the coordinator in the background, waits for
  GET /health, starts one worker in the background, then submits two jobs
  with submit.py --wait:
  1. `echo hello-farm && echo farm-output-content > out.txt`
  2. a job with an input file in.txt containing "roundtrip-42"; the command
     copies in.txt to copied.txt (use a portable shell command).
  Assert: both exit codes are 0; job 1's output file out.txt contains
  "farm-output-content"; job 2's output copied.txt contains "roundtrip-42".
  Write SMOKE_RESULT.txt into the repo directory with one PASS/FAIL line per
  check and a final line `SMOKE: PASS` or `SMOKE: FAIL`. Kill the background
  processes at the end. Exit non-zero if any check fails.

### README.md
- What this is (3-4 lines) and a tiny architecture sketch.
- Quickstart: set SHARECOMPUTE_TOKEN, start the coordinator, start a worker
  on the same machine, submit a sample job with submit.py.
- "Add a Windows machine" section: install Tailscale and join the same
  tailnet; copy worker.py (or build the exe with build_worker.bat); run the
  worker with the coordinator's Tailscale address and the shared token.
  The token is a shared secret: treat it like a password.
- Security note: the coordinator runs arbitrary shell commands for anyone
  holding the token. Only run it on a trusted private network (Tailscale);
  never expose it to the public internet.

## Definition of done
After writing all files, run ./smoke_test.sh yourself and fix whatever fails.
You are not done until SMOKE_RESULT.txt ends with `SMOKE: PASS`.
