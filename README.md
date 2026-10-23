# ShareCompute

Turn spare PCs into a personal compute farm: one host distributes work to many workers and collects the results. ShareCompute uses Python 3.10+ and the standard library only, with no runtime dependencies. Ship a folder of files out for parallel evaluation, then bring the outputs back to the host for merging.

## Architecture

```text
Host machine                              Worker machines
+-----------------------------+           +---------------------+
| submit.py / scatter_eval.py  |           | worker.py (node A)  |
|             |               | HTTP/JSON  | worker.py (node B)  |
| coordinator.py + jobs.json   |<--------->| worker.py (node C)  |
| collected outputs + summary |           +---------------------+
+-----------------------------+
```

The host runs the coordinator and submits jobs. Workers only register, poll for jobs, execute commands, and return results; each worker runs one job at a time. Run a worker on the host too if desired. Workers can announce `--label KEY=VALUE` labels (GPU model, RAM, …), and jobs can carry `--require`/`--worker` requirements so work lands only on machines that match.

Jobs move from `pending` to `assigned` to `done` or `failed`. Pending jobs can also become `cancelled`; they are never assigned to workers. CLI waiters return on cancellation (single-job CLI exit status 1). Each job runs in a temporary work directory with its attached inputs. Newly created files, including nested files, return with stdout, stderr, and the exit code. The work directory is removed afterward. Expired leases retry jobs up to three attempts; commands should tolerate repeated execution. The coordinator persists jobs atomically in `jobs.json`.

## Host quickstart

Copy this project onto a host with Python 3.10+. In a terminal:

```sh
export SHARECOMPUTE_TOKEN='replace-with-a-long-random-secret'
python3 coordinator.py
```

The coordinator listens on port 8765 on all interfaces; set `SC_PORT` to change it. Allow that port only on your private network. In another terminal, set the same token and submit a job once a worker is connected:

```sh
export SHARECOMPUTE_TOKEN='replace-with-a-long-random-secret'
python3 submit.py --coordinator http://127.0.0.1:8765 \
  --wait --wait-timeout 120 --outdir ./sc-out \
  -- 'echo hello-farm > greeting.txt'
```

Use `--file ./input.txt` to attach a file, or `--file ./input.txt=renamed.txt` to rename it. Repeat `--file` for multiple inputs. Without `--wait`, submission prints the job ID and returns immediately. With `--wait`, logs print and outputs are saved; the exit status follows the job's exit code. `--timeout` limits execution (default 3600 seconds). `--wait-timeout` limits waiting (default 0 means forever); expiration returns 124 and leaves the job running.

Target a job at specific machines with requirements. `--require KEY=VALUE` (repeatable) matches a worker label of the same key, and `--worker NAME` is shorthand for `--require name=NAME`:

```sh
python3 submit.py --coordinator http://127.0.0.1:8765 --require gpu=4060 \
  --wait -- 'nvidia-smi'
```

Jobs without requirements run on any worker, as before. A job whose requirements no online worker satisfies simply stays pending until a matching worker appears (or you cancel it).

On Windows PowerShell use `$env:SHARECOMPUTE_TOKEN = 'your-secret'` and `python` instead of `python3`. Commands run through the worker OS's shell, so use commands compatible with your worker machines.

## Dashboard

Start the coordinator as shown above (`python3 coordinator.py` with
`SHARECOMPUTE_TOKEN` set), open `http://<host>:8765/` in a browser, and paste
that token into the API token field. Keep `dashboard.html` next to
`coordinator.py`. The page loads no external resources and works on an offline
LAN; it uses inline CSS, vanilla JavaScript, and system fonts.

The dashboard shows worker presence, labels, and current assignments, job counts and
statuses (`pending`, `assigned`, `done`, `failed`, `cancelled`), and expandable
job details with logs, exit codes, requirements, output filenames and sizes. Submit commands
with a timeout, or cancel pending jobs. File attachments remain a CLI/scatter
feature. Finished-job durations measure execution time when assigned, or time
since creation for jobs cancelled before assignment.

Data refreshes every three seconds with a pause/play toggle. The token is saved
in this browser's localStorage and sent as `X-Token` on API calls. A rejected
token stops refresh until you change it. The page itself is public and contains
no job data; worker and job APIs require the token.

## Add a worker machine

Copy `worker.py` to any Linux, macOS, or Windows machine with Python 3.10+:

```sh
python3 worker.py --coordinator http://HOST_PRIVATE_IP:8765 --token your-secret
```

Use `python` on Windows. `--token` defaults to `SHARECOMPUTE_TOKEN`. Optional flags include `--name`, `--poll-interval` (default 5 seconds), and `--workdir` (default `./sc-work`). Announce labels with `--label KEY=VALUE` (repeatable) so jobs can target this machine:

```sh
python3 worker.py --coordinator http://HOST_PRIVATE_IP:8765 --token your-secret \
  --label gpu=4060 --label vram=8
```

Labels are stored at registration and refreshed on every poll and heartbeat, and they appear in the dashboard's workers table. A worker without `--label` behaves exactly as before. The label key `name` is reserved: in requirements it matches the worker's `--name`, not a label. Install the evaluation command and its dependencies on every worker; ShareCompute itself has no dependencies. Workers do not need the rest of this repository.

For Windows machines without Python, run `build_worker.bat` on a Windows build machine with Python installed. Install PyInstaller first with `python -m pip install pyinstaller`; the build script produces `dist\sharecompute-worker.exe`. Copy that executable to the worker and run:

```bat
sharecompute-worker.exe --coordinator http://HOST_PRIVATE_IP:8765 --token your-secret
```

## Scatter evaluation

Suppose `trajectories/` holds many independent trajectory files, and `evaluate-trajectories` is installed on each worker. The following submits sorted groups of 10 files, evaluates groups in parallel across available workers, and collects their outputs on the host:

```sh
python3 scatter_eval.py --coordinator http://127.0.0.1:8765 \
  --dir ./trajectories --shard-size 10 \
  --cmd 'evaluate-trajectories --output-dir {out} {files}' \
  --outdir ./scatter-out --timeout 3600 --wait-timeout 7200
```

`{files}` expands to the shard's basenames joined by spaces; `{out}` expands to `results`. The command must create `results` and write outputs there. Filename expansion is literal, so use simple filenames without spaces or shell metacharacters, or supply appropriate quoting in a command tailored to your filenames and worker shell. Subdirectories of the input folder are ignored. All input files are read before any job is submitted; a missing or unreadable file aborts the run.

A runnable POSIX-shell example that copies inputs to evaluated output names:

```sh
python3 scatter_eval.py --coordinator http://127.0.0.1:8765 \
  --dir ./trajectories --shard-size 2 \
  --cmd 'mkdir -p {out}; for f in {files}; do cp "$f" "{out}/$f.done"; done'
```

Outputs arrive under `scatter-out/shard-0/results/`, `shard-1/results/`, and so on. This keeps identically named outputs from different shards separate. `summary.json` is a JSON array containing each shard's zero-based `shard` index, `files`, `job_id`, `status`, and `exit_code`. The final line is `SCATTER: X/Y shards done`; exit 0 means every shard succeeded, and exit 1 indicates failure or an overall wait timeout. The wait deadline covers all shards together, starting after submission; it does not cancel remote jobs. An empty input directory produces an empty summary and `SCATTER: 0/0 shards done`.

`--require KEY=VALUE` and `--worker NAME` work here too and are attached to every shard job, so a whole scatter run can be pinned to machines with, say, a specific GPU. Shards are still submitted in sorted filename order, but ordering tricks are no longer needed for pinning: pinning is per-job through requirements, and unmatched jobs simply stay pending until a matching worker appears.

After all shards finish (regardless of pass/fail), `--merge-cmd 'CMD'` runs CMD once on the host with the shell, with `SC_SUMMARY` pointing at the written `summary.json` and `SC_OUTDIR` at the outdir, and its exit code prints as `MERGE: exit N`:

```sh
python3 scatter_eval.py --coordinator http://127.0.0.1:8765 \
  --dir ./trajectories --shard-size 10 \
  --cmd 'evaluate-trajectories --output-dir {out} {files}' \
  --merge-cmd 'python3 merge-all-shards.py "$SC_OUTDIR"' \
  --outdir ./scatter-out
```

A failing merge command makes scatter exit 1 even when every shard succeeded; without `--merge-cmd` nothing changes. It replaces ad-hoc post-run merge scripts, for example:

```sh
python3 - <<'PY'
import json
from pathlib import Path
results = [json.loads(p.read_text()) for p in sorted(Path('scatter-out').glob('shard-*/results/*.json'))]
Path('merged-results.json').write_text(json.dumps(results, indent=2))
PY
```

## Scope and limits

This parallelizes independent jobs; it does not speed up a single training run or provide distributed GPU training. There is no dependency installation, environment replication, sandbox, or built-in result-format merging (bring your own via `--merge-cmd`). Inputs are not returned as outputs, even if modified. Symlinks are excluded from output collection.

Individual output files over 50 MiB are skipped. Before sending a result, workers drop the largest attachments until its JSON fits 100 MiB, recording dropped names in stderr. A rejected result (any HTTP 4xx) gets one retry with no attachments before the worker moves on; expired leases handle abandoned jobs. The coordinator rejects request bodies over 120 MiB with `{"error": "payload_too_large"}` and keeps the last 200 KiB of each log. Large datasets should use suitably small shards. A successful command can still have omitted outputs, so inspect logs and expected artifacts.

## Security

The token is a shared secret and the protocol uses plain HTTP. Run ShareCompute on a trusted private network, such as Tailscale; **never expose the coordinator to the public internet**. Anyone with the token can submit arbitrary shell commands that run with the worker account's permissions. Only connect machines and users you trust. Protect the token, `jobs.json`, and returned data.

## Project files and tests

- `coordinator.py`: persistent HTTP coordinator.
- `dashboard.html`: self-contained offline web dashboard.
- `worker.py`: standalone compute node.
- `submit.py`: single-job CLI.
- `sc_client.py`: shared submission, lookup, and wait functions; file maps hold base64 strings.
- `scatter_eval.py`: folder sharding and result collection.
- `build_worker.bat`: optional Windows executable build.
- `test_sharecompute.py`, `smoke_test.sh`: regression and end-to-end tests.

```sh
python3 -m unittest -v test_sharecompute
./smoke_test.sh
```

The smoke test starts a temporary coordinator and a labeled worker, verifies the dashboard, online worker listing with labels, completion and cancellation (including non-execution), a targeted job (`--require gpu=smoke`), a job with an unsatisfiable requirement staying pending and then cancelled, submission and file round trips, then checks two scatter shards with four files plus the `--merge-cmd` hook. `SMOKE_RESULT.txt` ends with `SMOKE: PASS` on success. The shell smoke test needs Bash and `timeout` (for example, Linux or WSL).

Licensed under the [MIT License](LICENSE).
