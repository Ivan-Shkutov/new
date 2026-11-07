#!/usr/bin/env python3
"""Upgrade a verified complete release, keeping the active runtime and rollback.

Standard library only. Does not modify Astra, vLLM, Kubernetes or model settings.
Run from the unpacked release after verifying the outer archive SHA256.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def execute(args, capture=False):
    return subprocess.run([str(x) for x in args], check=True, text=True,
                          capture_output=capture, timeout=900)


def verify_manifest(root):
    lines = (root / 'MANIFEST.sha256').read_text().splitlines()
    if not lines:
        raise ValueError('Empty release manifest')
    seen = set()
    for line in lines:
        expected, name = line.split('  ', 1)
        path = root / name
        if (name in seen or len(expected) != 64 or Path(name).is_absolute()
                or '..' in Path(name).parts or not path.resolve().is_relative_to(root.resolve())
                or path.is_symlink() or not path.is_file()):
            raise ValueError('Unsafe or missing release member')
        seen.add(name)
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(block)
        if digest.hexdigest() != expected:
            raise ValueError('Release file hash mismatch: ' + name)
    if 'RELEASE.json' not in seen:
        raise ValueError('Release identity is missing from manifest')
    return json.loads((root / 'RELEASE.json').read_text())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--container', default='pulse-clusters-8090-pulse-1')
    parser.add_argument('--backup-dir', type=Path)
    parser.add_argument('--current-link', type=Path, help='Optional existing source symlink to switch after a verified upgrade')
    args = parser.parse_args(argv)
    stopped = False
    previous = runtime = None
    try:
        identity = verify_manifest(ROOT)
        container = json.loads(execute(['docker', 'inspect', args.container], capture=True).stdout)[0]
        if not container.get('State', {}).get('Running'):
            raise ValueError('Current Pulse must be running before this upgrade')
        labels = container['Config'].get('Labels') or {}
        old_path = labels.get('com.docker.compose.project.working_dir')
        mounts = [m['Source'] for m in container.get('Mounts', []) if m.get('Destination') == '/runtime']
        if not old_path or len(mounts) != 1:
            raise ValueError('Cannot uniquely identify previous source and runtime')
        previous, runtime = Path(old_path).resolve(), Path(mounts[0]).resolve()
        if previous == ROOT.resolve():
            raise ValueError('This release directory is already active')
        if not (previous / 'pulse').is_file() or not (runtime / 'registry.key').is_file():
            raise ValueError('Previous CLI or original runtime key is missing')
        current_link=args.current_link
        if current_link is not None and (not current_link.is_symlink() or current_link.resolve()!=previous):
            raise ValueError('current-link must point to the active previous release')
        version = identity['version']
        if container['Config'].get('Image') == 'pulse-clusters:' + version:
            raise ValueError('This version tag is already active; refusing to overwrite the rollback image')
        env = dict(line.split('=', 1) for line in (runtime / '.env').read_text().splitlines()
                   if line and not line.startswith('#') and '=' in line)
        port = env.get('PULSE_PORT', '8090')
        if (not port.isdigit() or not 1024 <= int(port) <= 65535
                or labels.get('com.docker.compose.project') != 'pulse-clusters-' + port
                or labels.get('com.docker.compose.service') != 'pulse'):
            raise ValueError('Active container and saved runtime port/project do not match')
        image = ROOT / 'image' / ('pulse-clusters-' + version + '.tar.gz')
        if image.is_file():
            execute(['docker', 'image', 'load', '--input', image])
        else:
            execute([ROOT / 'pulse', '--runtime', runtime, 'build'])
        # Refuse stale/mistagged images before interrupting the running service.
        probe = execute(['docker', 'run', '--rm', '--network=none', '--read-only',
                         '--cap-drop=ALL', '--security-opt=no-new-privileges:true',
                         '--entrypoint=python', 'pulse-clusters:' + version, '-c',
                         'import json;from pulse_app.build import identity;print(json.dumps(identity()))'], capture=True)
        measured = json.loads(probe.stdout)
        if any(measured.get(k) != identity[k] for k in ('version', 'build_id')):
            raise ValueError('Docker image does not match release version/build')
        proposed_backup = args.backup_dir or (Path.home() / ('pulse-backup-' + version + '-' +
                    datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')))
        if proposed_backup.is_symlink():raise ValueError('Backup directory must not be a symlink')
        backup=proposed_backup.resolve()
        if backup.is_relative_to(runtime):
            raise ValueError('Backup directory must be outside runtime')
        if backup.is_symlink() or (backup.exists() and (not backup.is_dir() or any(backup.iterdir()))):
            raise ValueError('Backup directory must be new or empty')
        backup.mkdir(mode=0o700, parents=True, exist_ok=True)
        backup.chmod(0o700)
        state = {'previous_source': str(previous), 'runtime': str(runtime), 'new_source': str(ROOT),
                 'version': version, 'build_id': identity['build_id'], 'previous_image': container['Config']['Image']}
        rollback = '#!/bin/sh\nset -eu\n' + '\n'.join(
            ' '.join(shlex.quote(str(x)) for x in command) for command in (
                (ROOT / 'pulse', '--runtime', runtime, 'stop'),
                (previous / 'pulse', '--runtime', runtime, 'start-offline'),
                (previous / 'pulse', '--runtime', runtime, 'check'))) + '\n'
        if current_link is not None:
            rollback+='ln -sfn '+shlex.quote(str(previous))+' '+shlex.quote(str(current_link))+ '\n'
        (backup / 'upgrade.json').write_text(json.dumps(state, indent=2) + '\n')
        (backup / 'rollback.sh').write_text(rollback)
        (backup / 'rollback.sh').chmod(0o700)
        print('Backup directory:', backup, flush=True)
        stopped = True  # Backup/stop may interrupt service before returning failure.
        execute([previous / 'pulse', '--runtime', runtime, 'backup', '--output-dir', backup])
        execute([previous / 'pulse', '--runtime', runtime, 'stop'])
        execute([ROOT / 'pulse', '--runtime', runtime, 'start-offline'])
        execute([ROOT / 'pulse', '--runtime', runtime, 'check'])
        if current_link is not None:
            temporary=current_link.with_name(current_link.name+'.upgrade-new')
            if temporary.is_symlink():temporary.unlink()
            temporary.symlink_to(ROOT.resolve())
            temporary.replace(current_link)
        print('Upgrade verified:', version, identity['build_id'])
        print('Runtime retained:', runtime)
        print('Rollback:', backup / 'rollback.sh')
        return 0
    except (ValueError, KeyError, IndexError, OSError, subprocess.SubprocessError) as error:
        print('Upgrade failed:', error, file=sys.stderr)
        if stopped and previous and runtime:
            try:
                execute([ROOT / 'pulse', '--runtime', runtime, 'stop'])
                execute([previous / 'pulse', '--runtime', runtime, 'start-offline'])
                execute([previous / 'pulse', '--runtime', runtime, 'check'])
                print('Previous release restarted with the original runtime.', file=sys.stderr)
            except (OSError, subprocess.SubprocessError) as rollback_error:
                print('Automatic rollback failed:', rollback_error, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
