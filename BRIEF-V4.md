# ShareCompute v4 — targeted assignment, worker labels, scatter merge hook

You are working in ~/sharecompute (v3 complete, smoke PASS: coordinator.py,
worker.py, submit.py, scatter_eval.py, sc_client.py, dashboard.html,
build_worker.bat, smoke_test.sh, test_sharecompute.py, README.md).
Python 3.10+ standard library ONLY, no pip dependencies, dashboard makes
zero external requests. All existing behavior and tests must keep passing.

Tonight this farm ran a real dual-GPU split-training test (laptop 4060 +
cloud 5090). Three gaps hurt in practice. Fix exactly these three:

## 1. Worker labels

- worker.py gains `--label KEY=VALUE` (repeatable). Labels are stored at
  registration and refreshed on every poll/heartbeat; the coordinator
  keeps them per worker and includes them in GET /workers output.
- A worker with no labels behaves exactly as before.

## 2. Targeted jobs (requirements)

- submit.py and scatter_eval.py gain `--require KEY=VALUE` (repeatable)
  and `--worker NAME` (sugar for requiring that exact worker name).
- The job record stores `requires` (dict, possibly empty) and the
  coordinator's /poll must only hand a pending job to a worker that
  satisfies ALL requirements: each KEY=VALUE matches the worker's label
  of the same key; the special key `name` matches the worker's name.
- Jobs with empty requirements go to any worker, as today.
- A job whose requirements no registered worker satisfies simply stays
  pending (no error). Dashboard: show a job's requirements in its detail
  panel, and worker labels in the workers section. Keep the page
  self-contained and XSS-safe (textContent only), size sane.

## 3. Scatter merge hook

- scatter_eval.py gains `--merge-cmd 'CMD'`: after all shards finish
  (regardless of pass/fail), if given, run CMD once on the host with the
  shell, with environment variable SC_SUMMARY pointing at the written
  summary.json path and SC_OUTDIR at the outdir. Print its exit code as
  `MERGE: exit N`. A failing merge command makes scatter exit 1; no
  merge command = behavior unchanged.

## Tests (extend smoke_test.sh, keep every existing check)

- Start the smoke worker with `--label gpu=smoke`.
- Submit job C with `--require gpu=smoke`: must complete.
- Submit job D with `--require gpu=nonexistent`: after 10 s it must
  still be pending (assert via GET /jobs/{id}); then cancel it via the
  cancel endpoint to keep the suite clean.
- Run the existing scatter part with `--merge-cmd 'cp "$SC_SUMMARY"
  merged-summary.json'` run from the smoke outdir context — assert
  merged-summary.json exists next to summary.json afterwards and
  `MERGE: exit 0` was printed.
All lines PASS/FAIL in SMOKE_RESULT.txt, last line `SMOKE: PASS`.
Keep test_sharecompute.py green and add unit tests for the requirement
matcher (empty requires, label match, label mismatch, name match).

## README.md
Document labels, --require/--worker, and --merge-cmd with one short
example each, and note the RUNBOOK ordering trick is no longer needed
for pinning shards (scatter still assigns shards in order; pinning is
per-job via requirements).

## Definition of done
./smoke_test.sh passes end-to-end with the new checks, all .py files
compile (py_compile), existing behavior unchanged. Do not finish
before that. Work autonomously; do not ask questions.
