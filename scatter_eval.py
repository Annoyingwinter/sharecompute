#!/usr/bin/env python3
"""Distribute a folder's files across jobs and collect results on the host."""
import argparse
import base64
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time

from sc_client import parse_requires, submit_job, wait_job
from submit import output_path
from worker import safe_name


def run_merge(command, outdir):
    """Run the merge hook in outdir with SC_SUMMARY/SC_OUTDIR set; True on exit 0."""
    env = dict(os.environ, SC_SUMMARY=str(outdir / 'summary.json'), SC_OUTDIR=str(outdir))
    code = subprocess.call(command, shell=True, cwd=outdir, env=env)
    print(f'MERGE: exit {code}')
    return code == 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--coordinator', required=True)
    parser.add_argument('--token', default=os.environ.get('SHARECOMPUTE_TOKEN'))
    parser.add_argument('--dir', required=True)
    parser.add_argument('--cmd', required=True)
    parser.add_argument('--shard-size', type=int, default=1)
    parser.add_argument('--outdir', default='./scatter-out')
    parser.add_argument('--timeout', type=float, default=3600)
    parser.add_argument('--wait-timeout', type=float, default=0)
    parser.add_argument('--require', action='append', default=[], metavar='KEY=VALUE',
                        help='only workers whose label KEY equals VALUE may run the shards; repeatable')
    parser.add_argument('--worker', metavar='NAME', help='target one worker by name (same as --require name=NAME)')
    parser.add_argument('--merge-cmd', metavar='CMD',
                        help='shell command run on the host after all shards finish; '
                             'SC_SUMMARY and SC_OUTDIR point at summary.json and the outdir')
    args = parser.parse_args()
    if not args.token:
        parser.error('--token or SHARECOMPUTE_TOKEN is required')
    if args.shard_size <= 0:
        parser.error('--shard-size must be positive')
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error('--timeout must be finite and positive')
    if not math.isfinite(args.wait_timeout) or args.wait_timeout < 0:
        parser.error('--wait-timeout must be finite and nonnegative')
    try:
        requires = parse_requires(args.require)
    except ValueError as exc:
        parser.error(str(exc))
    if args.worker:
        if 'name' in requires:
            parser.error('--worker conflicts with --require name=...')
        requires['name'] = args.worker
    records = []
    try:
        # Read every input before the first POST, including broken symlinks.
        files = {}
        for path in sorted(Path(args.dir).iterdir()):
            if path.is_dir():
                continue
            if not safe_name(path.name):
                raise ValueError(f'Unsafe input filename: {path.name!r}')
            if path.exists() and not path.is_file():
                raise ValueError(f'Input is not a regular file: {path}')
            files[path.name] = base64.b64encode(path.read_bytes()).decode('ascii')
        root = Path(args.outdir).resolve()
        root.mkdir(parents=True, exist_ok=True)
        names = list(files)
        for offset in range(0, len(names), args.shard_size):
            shard = names[offset:offset + args.shard_size]
            # Only replace the two documented placeholders; preserve shell braces.
            command = args.cmd.replace('{out}', 'results').replace('{files}', ' '.join(shard))
            records.append(dict(shard=len(records), files=shard, job_id=None,
                                status='not_submitted', exit_code=None))
            records[-1]['job_id'] = submit_job(args.coordinator, args.token, command,
                                             {name: files[name] for name in shard}, args.timeout, requires)
            records[-1]['status'] = 'pending'
        deadline = time.monotonic() + args.wait_timeout if args.wait_timeout else None
        for record in records:
            remaining = deadline - time.monotonic() if deadline else 0
            try:
                if deadline and remaining <= 0:
                    raise TimeoutError('Overall scatter wait timed out')
                job = wait_job(args.coordinator, args.token, record['job_id'],
                               wait_timeout=remaining)
                record.update(status=job['status'], exit_code=job['exit_code'])
                shard_root = root / f'shard-{record["shard"]}'
                shard_root.mkdir(parents=True, exist_ok=True)
                for name, body in job['output_files'].items():
                    target = output_path(shard_root.resolve(), name)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(base64.b64decode(body, validate=True))
            except (OSError, ValueError) as exc:
                record['status'] = 'wait_timeout' if isinstance(exc, TimeoutError) else 'error'
                print(f'Shard {record["shard"]}: {exc}', file=sys.stderr)
        (root / 'summary.json').write_text(json.dumps(records, indent=2) + '\n', encoding='utf-8')
        done = sum(r['status'] == 'done' and r['exit_code'] == 0 for r in records)
        for record in records:
            print(f'Shard {record["shard"]}: {record["status"]}, exit_code={record["exit_code"]}, job_id={record["job_id"]}')
        print(f'SCATTER: {done}/{len(records)} shards done')
        merge_ok = not args.merge_cmd or run_merge(args.merge_cmd, root)
        return 0 if done == len(records) and merge_ok else 1
    except (OSError, ValueError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
