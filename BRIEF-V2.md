# ShareCompute v2 — generalize into a main/worker open-source project

You are working in ~/sharecompute, which already contains a working v1
(coordinator.py, worker.py, submit.py, build_worker.bat, smoke_test.sh,
test_sharecompute.py, README.md, BRIEF.md). v1 smoke passes. Now evolve it
into a generalized, open-source-shaped compute farm:

- ONE host machine runs the coordinator and submits work.
- Any number of worker machines ("nodes") only register, take jobs, run
  them, and return results — they provide compute to the host.
- The flagship workflow is "ship files out, evaluate in parallel, collect
  results back": the host splits a folder of files into shards, each worker
  evaluates its shard with the same command, and all outputs return to the
  host for merging.

Keep: Python 3.10+ stdlib only, the existing HTTP+JSON+token protocol and
file layout. Extend, do not rewrite from scratch.

## Required changes

1. Fix the 413 deadlock (real bug found in review):
   - Worker side: before POST /result, estimate the JSON payload size. If it
     would exceed 100 MB, drop the largest output_files entries one by one
     (append a note to stderr listing dropped file names) until it fits.
   - If POST /result still returns 413 (or any 4xx), the worker must NOT
     retry forever: it retries once with output_files emptied and a stderr
     note, then gives up on that job and moves on (the coordinator lease
     will handle the rest).
   - Coordinator side: keep the 120 MB body guard, but answer 413 with a
     JSON body {"error": "payload_too_large"}.

2. submit.py: `--wait` gains `--wait-timeout SECONDS` (default 0 = wait
   forever). On timeout, print a clear message and exit with code 124.

3. New file sc_client.py: a tiny shared client used by submit.py and
   scatter_eval.py — functions: submit_job(coord, token, command, files,
   timeout_sec) -> job_id; get_job(coord, token, job_id) -> dict;
   wait_job(coord, token, job_id, poll=2, wait_timeout=0) -> final job
   dict. Refactor submit.py to use it without changing its CLI behavior.

4. New file scatter_eval.py — the flagship workflow:
   - Args: --coordinator, --token (default env SHARECOMPUTE_TOKEN),
     --dir DIR (folder of files to distribute, non-recursive),
     --cmd TEMPLATE, --shard-size N (files per job, default 1),
     --outdir DIR (default ./scatter-out), --timeout SECONDS (per job,
     default 3600), --wait-timeout SECONDS (overall, default 0 = forever).
   - TEMPLATE placeholders: {files} = the shard's file names joined by
     spaces; {out} = the literal directory name "results" (the command is
     expected to create it and put its outputs there; all new files in the
     job workdir are returned by the existing worker mechanism anyway).
   - Behavior: sort the file list; chunk into shards of N; for each shard
     submit one job with the shard's files attached and the rendered
     command; wait for all jobs; download each job's output files into
     outdir/shard-<index>/; write outdir/summary.json containing, per
     shard: shard index, file names, job_id, status, exit_code; print a
     one-line-per-shard summary plus a final line
     "SCATTER: X/Y shards done"; exit 0 only if all shards succeeded,
     else exit 1.
   - A missing/unreadable input file must abort before submitting anything,
     with a clear error.

5. README.md rewrite (open-source facing):
   - Title + one-paragraph pitch: turn spare PCs into a personal compute
     farm — one host, many workers, stdlib-only Python, no dependencies.
   - Architecture: host (coordinator + submit/scatter) vs workers (dumb
     nodes), ASCII diagram, the job lifecycle in a few lines.
   - Quickstart for the host; "Add a worker machine" (any OS: run
     worker.py with the host's address + token; or the PyInstaller exe on
     Windows via build_worker.bat).
   - A "Scatter evaluation" section with a concrete example: a folder of
     trajectories + an eval command -> parallel evaluation -> merged
     outputs + summary.json on the host.
   - Honest scope note: this parallelizes independent jobs; it does not
     speed up a single training run.
   - Security: token is a shared secret, plain HTTP — run it on a trusted
     private network (e.g. Tailscale); never expose the coordinator to
     the public internet.

6. Open-source readiness: add LICENSE (MIT, copyright holder "ShareCompute
   contributors"), .gitignore (jobs.json, sc-work/, scatter-out/,
   __pycache__/, *.log), and keep BRIEF.md/BRIEF-V2.md out of the README's
   file list (they are build notes).

7. Smoke: extend smoke_test.sh with a scatter check — create eval-data/
   with 4 files f1.txt..f4.txt (contents data-1..data-4); run
   scatter_eval.py --shard-size 2 with a command that copies each input
   file into results/ with a ".done" suffix (portable shell); assert:
   exit code 0, outdir has shard-0 and shard-1 outputs, all 4 transformed
   files present with correct contents, summary.json exists and reports
   2/2 shards done. Keep all existing checks. SMOKE_RESULT.txt must still
   end with `SMOKE: PASS`. Also run test_sharecompute.py if it is a test
   suite and keep it green.

## Definition of done
All files written, ./smoke_test.sh passes end-to-end (SMOKE_RESULT.txt
last line `SMOKE: PASS`), existing unit tests still pass. Do not finish
before that.
