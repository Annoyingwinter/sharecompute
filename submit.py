#!/usr/bin/env python3
"""Submit a shell job to ShareCompute, optionally waiting for its results."""
import argparse
import base64
import math
import os
from pathlib import Path, PureWindowsPath
import sys


from sc_client import parse_requires, submit_job, wait_job


def output_path(root, name):
    if (not name or '\\' in name or ':' in name or '\x00' in name or '..' in name
            or Path(name).is_absolute() or PureWindowsPath(name).is_absolute()):
        raise ValueError(f'Unsafe output filename: {name!r}')
    target = (root / name).resolve()
    if not target.is_relative_to(root):
        raise ValueError(f'Output escapes outdir: {name!r}')
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--coordinator', required=True)
    parser.add_argument('--token', default=os.environ.get('SHARECOMPUTE_TOKEN'))
    parser.add_argument('--timeout', type=float, default=3600)
    parser.add_argument('--file', action='append', default=[])
    parser.add_argument('--require', action='append', default=[], metavar='KEY=VALUE',
                        help='only workers whose label KEY equals VALUE may run this job; repeatable')
    parser.add_argument('--worker', metavar='NAME', help='target one worker by name (same as --require name=NAME)')
    parser.add_argument('--wait-timeout', type=float, default=0)
    parser.add_argument('--wait', action='store_true')
    parser.add_argument('--outdir', default='./sc-out')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not args.token:
        parser.error('--token or SHARECOMPUTE_TOKEN is required')
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error('--timeout must be finite and positive')
    if not math.isfinite(args.wait_timeout) or args.wait_timeout < 0:
        parser.error('--wait-timeout must be finite and nonnegative')
    command = args.command
    if command and command[0] == '--':
        command = command[1:]
    if not command:
        parser.error('provide a shell command after --')
    try:
        requires = parse_requires(args.require)
    except ValueError as exc:
        parser.error(str(exc))
    if args.worker:
        if 'name' in requires:
            parser.error('--worker conflicts with --require name=...')
        requires['name'] = args.worker
    try:
        files = {}
        for spec in args.file:
            source, sep, name = spec.partition('=')
            if not sep:
                name = Path(source).name
            if not name or any(c in name for c in ('/', '\\', '..', ':', '\x00')):
                raise ValueError('input names must be simple basenames without ..')
            if name in files:
                raise ValueError(f'Duplicate input filename: {name}')
            files[name] = base64.b64encode(Path(source).read_bytes()).decode('ascii')
        job_id = submit_job(args.coordinator, args.token, ' '.join(command), files, args.timeout, requires)
        print(job_id, flush=True)
        if not args.wait:
            return 0
        job = wait_job(args.coordinator, args.token, job_id, wait_timeout=args.wait_timeout)
        print(job['stdout'], end='', flush=True)
        print(job['stderr'], end='', file=sys.stderr, flush=True)
        root = Path(args.outdir).resolve()
        root.mkdir(parents=True, exist_ok=True)
        for name, body in job['output_files'].items():
            target = output_path(root, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(base64.b64decode(body, validate=True))
        code = job['exit_code']
        if code is None:
            return 1
        return code if code >= 0 else 128 - code
    except TimeoutError as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 124
    except (OSError, ValueError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
