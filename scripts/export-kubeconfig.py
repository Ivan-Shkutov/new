#!/usr/bin/env python3
"""Export scoped standalone kubeconfig using current kubectl admin authentication."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.parse


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--context', required=True)
    parser.add_argument('--duration', default='24h')
    parser.add_argument('--server')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Output exists; use a new name to avoid overwriting a credential')
    base = ['kubectl', '--context', args.context]
    raw = subprocess.run(base + ['config', 'view', '--raw', '--minify', '--flatten', '-o', 'json'], check=True, capture_output=True, text=True)
    config = json.loads(raw.stdout)
    cluster = config['clusters'][0]['cluster']
    server = args.server or cluster['server']
    parsed = urllib.parse.urlsplit(server)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        parser.error('Server must be an HTTPS endpoint without credentials')
    if cluster.get('insecure-skip-tls-verify') or not cluster.get('certificate-authority-data'):
        parser.error('A verified cluster with embedded CA is required')
    token = subprocess.run(base + ['-n', 'pulse-observer', 'create', 'token', 'pulse-reader', '--duration', args.duration], check=True, capture_output=True, text=True).stdout.strip()
    result = {
        'apiVersion': 'v1', 'kind': 'Config',
        'clusters': [{'name': 'pulse', 'cluster': {'server': server, 'certificate-authority-data': cluster['certificate-authority-data']}}],
        'users': [{'name': 'pulse-reader', 'user': {'token': token}}],
        'contexts': [{'name': 'pulse', 'context': {'cluster': 'pulse', 'user': 'pulse-reader'}}],
        'current-context': 'pulse',
    }
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(result, stream, indent=2)
    print(f'Wrote {args.output}. Contains a credential; keep outside Git. Requested duration: {args.duration}; API may shorten it.')


if __name__ == '__main__':
    try:
        main()
    except subprocess.CalledProcessError:
        print('kubectl failed; check context, RBAC and API connectivity. Captured credentials are not printed.', file=sys.stderr)
        sys.exit(1)
