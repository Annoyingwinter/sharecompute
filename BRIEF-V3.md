# ShareCompute v3 — built-in web dashboard

You are working in ~/sharecompute (v2 complete, smoke PASS: coordinator.py,
worker.py, submit.py, scatter_eval.py, sc_client.py, build_worker.bat,
smoke_test.sh, test_sharecompute.py, README.md). Add a visual web
dashboard. Keep the project rules: Python 3.10+ stdlib only, no pip
dependencies, and the frontend must make ZERO external requests (no CDNs,
no webfonts — it has to work on an offline LAN).

## coordinator.py changes (extend, keep all existing behavior)

1. GET / -> serve dashboard.html (the file sitting next to
   coordinator.py) with Content-Type text/html; charset=utf-8. No auth
   for the page itself (it contains no data). If the file is missing,
   return 500 with a short plain-text note.
2. GET /workers (auth required) -> JSON list of registered workers:
   {id, name, platform, last_seen (epoch seconds), age_sec,
    online (last_seen within 45 s),
    current_job (the id of a job currently assigned to this worker,
    or null)}. Sort: online first, then name.
3. POST /jobs/{id}/cancel (auth required) -> only a job in status
   "pending" can be cancelled: set status "cancelled", finished_at=now,
   persist. Return the job summary. If the job is not pending, return
   409 with {"error": "not_cancellable"}. Unknown id -> 404.
   "cancelled" joins the status enum everywhere it matters (job listing,
   README). Cancelled jobs are never handed out by /poll (they are not
   pending, so this follows automatically — keep it that way).

## dashboard.html (new, single file, inline CSS + vanilla JS)

A single self-contained page. Requirements:
- Token handling: an input for the API token at the top; the value is
  kept in localStorage and sent as the X-Token header on every API
  call. A 401 from any call shows a clear banner "token missing or
  wrong" and stops the refresh loop until the token changes.
- Header: product name, a health dot (green when GET /health answers),
  last-updated time, auto-refresh every 3 seconds with a pause/play
  toggle.
- Stat cards: Workers online (x/y), Pending, Running, Done, Failed —
  computed from /workers and /jobs.
- Workers section: table or cards with name, platform, online/offline
  dot, last seen as relative time ("12s ago"), current job short id.
- Jobs section: table with short id (first 8 chars), command
  (truncated to ~80 chars, full text in the title attribute), status
  badge with a distinct color per status (pending, assigned, done,
  failed, cancelled), worker name, attempts, created time, and
  duration for finished jobs. Sorted newest first. Clicking a row
  expands a detail panel below it: full command, exit code, stdout and
  stderr in <pre> blocks, output file names with byte sizes, and
  last_error when present. A Cancel button appears in the detail panel
  (and/or row) for pending jobs and calls the cancel endpoint.
- Submit form: command textarea + timeout seconds + submit button.
  On success, show the new job id and let the table refresh highlight
  it. (Attaching files stays a CLI/scatter feature — say so in one
  small line under the form.)
- XSS hygiene: job commands, outputs, and names are arbitrary text.
  Build DOM with textContent / createElement, never innerHTML with
  unescaped data.
- Style: clean dark theme, system font stack, one accent color,
  readable on a phone screen, no frameworks, total file size sane
  (aim < 60 KB).

## README.md
Add a "Dashboard" section: start the coordinator, open
http://<host>:8765/ in a browser, paste the token. List what it shows
(workers, jobs, submit, cancel) and note the page loads no external
resources. Add dashboard.html to the file list.

## Smoke (extend smoke_test.sh, keep every existing check)
New checks after the coordinator is up and a worker is registered:
- GET / returns 200 and the body contains "ShareCompute".
- GET /workers with the token lists the smoke worker with
  online == true.
- Submit job A (a sleep-free trivial command) and let it complete.
- Submit job B, cancel it via POST /jobs/{id}/cancel, then assert
  GET /jobs/{id} reports status "cancelled" and job B's marker file
  never appears (the worker must not run it).
Write the new checks as PASS/FAIL lines in SMOKE_RESULT.txt like the
others; the last line must remain `SMOKE: PASS`. Keep
test_sharecompute.py green if it is a runnable suite.

## Definition of done
./smoke_test.sh passes end-to-end with the new checks, all .py files
compile (py_compile), existing behavior unchanged. Do not finish
before that.
