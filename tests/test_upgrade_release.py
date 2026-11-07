"""Upgrade preflight and rollback: never interrupt service for an invalid release."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'upgrade-existing.py'
spec = importlib.util.spec_from_file_location('pulse_upgrade_release', SCRIPT)
upgrade = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upgrade)


def setup(tmp_path, monkeypatch, fail=False, wrong_image=False):
    root, old, runtime = [tmp_path / n for n in ('new', 'old', 'runtime')]
    for p in (root, old, runtime): p.mkdir()
    for p in (root / 'pulse', old / 'pulse', runtime / 'registry.key'): p.write_text('fixture')
    (runtime / '.env').write_text('PULSE_PORT=8090\n')
    identity = {'version': 'fixture', 'build_id': 'exact-build'}
    (root / 'RELEASE.json').write_text(json.dumps(identity))
    (root / 'MANIFEST.sha256').write_text(''.join(
        hashlib.sha256((root / n).read_bytes()).hexdigest() + '  ' + n + '\n'
        for n in ('pulse', 'RELEASE.json')))
    monkeypatch.setattr(upgrade, 'ROOT', root)
    calls = []
    container = {'State': {'Running': True}, 'Config': {'Image': 'pulse:old', 'Labels': {
        'com.docker.compose.project.working_dir': str(old),
        'com.docker.compose.project': 'pulse-clusters-8090', 'com.docker.compose.service': 'pulse'}},
        'Mounts': [{'Source': str(runtime), 'Destination': '/runtime'}]}
    def execute(args, capture=False):
        args = [str(a) for a in args];calls.append(args)
        if args[:2] == ['docker', 'inspect']: return SimpleNamespace(stdout=json.dumps([container]))
        if args[:2] == ['docker', 'run']:
            return SimpleNamespace(stdout=json.dumps({**identity, 'build_id': 'wrong'} if wrong_image else identity))
        if fail and args[0] == str(root / 'pulse') and args[-1] == 'check':
            raise subprocess.CalledProcessError(1, args)
        return SimpleNamespace(stdout='')
    monkeypatch.setattr(upgrade, 'execute', execute)
    return root, old, runtime, calls, container


def test_success_keeps_original_runtime_and_creates_private_rollback(tmp_path, monkeypatch):
    root, old, runtime, calls, _ = setup(tmp_path, monkeypatch)
    backup = tmp_path / 'backup'
    assert upgrade.main(['--backup-dir', str(backup)]) == 0
    assert json.loads((backup / 'upgrade.json').read_text())['runtime'] == str(runtime)
    assert (backup / 'rollback.sh').stat().st_mode & 0o777 == 0o700
    actions = [c for c in calls if c[0] in (str(old / 'pulse'), str(root / 'pulse'))]
    assert [c[-1] if c[-1] != str(backup) else 'backup' for c in actions] == [
        'build', 'backup', 'stop', 'start-offline', 'check']
    assert all(c[1:3] == ['--runtime', str(runtime)] for c in actions)


def test_failed_new_release_restarts_previous_without_restore_or_init(tmp_path, monkeypatch):
    root, old, runtime, calls, _ = setup(tmp_path, monkeypatch, fail=True)
    assert upgrade.main(['--backup-dir', str(tmp_path / 'backup')]) == 1
    assert calls[-2:] == [[str(old / 'pulse'), '--runtime', str(runtime), 'start-offline'],
                         [str(old / 'pulse'), '--runtime', str(runtime), 'check']]
    assert not any('restore' in c or 'init' in c for c in calls)


def test_empty_precreated_backup_is_accepted_but_nonempty_is_not(tmp_path,monkeypatch):
    _,_,_,calls,_=setup(tmp_path,monkeypatch)
    backup=tmp_path/'backup';backup.mkdir()
    assert upgrade.main(['--backup-dir',str(backup)])==0
    calls.clear()
    assert upgrade.main(['--backup-dir',str(backup)])==1
    assert not any(c[-1]=='stop' for c in calls)


def test_verified_upgrade_switches_current_link_and_rollback_restores_it(tmp_path,monkeypatch):
    root,old,_,_,_=setup(tmp_path,monkeypatch)
    link=tmp_path/'current';link.symlink_to(old)
    backup=tmp_path/'backup'
    assert upgrade.main(['--backup-dir',str(backup),'--current-link',str(link)])==0
    assert link.resolve()==root
    assert 'ln -sfn' in (backup/'rollback.sh').read_text()


@pytest.mark.parametrize('case', ['modified', 'wrong-image', 'missing-key', 'ambiguous-mount', 'stopped', 'same-version-tag', 'changed-port'])
def test_preflight_failure_does_not_stop_active_container(tmp_path, monkeypatch, case):
    root, _, runtime, calls, container = setup(tmp_path, monkeypatch, wrong_image=case == 'wrong-image')
    if case == 'modified': (root / 'pulse').write_text('modified')
    if case == 'missing-key': (runtime / 'registry.key').unlink()
    if case == 'ambiguous-mount': container['Mounts'] *= 2
    if case == 'stopped': container['State']['Running'] = False
    if case == 'same-version-tag': container['Config']['Image'] = 'pulse-clusters:fixture'
    if case == 'changed-port': (runtime / '.env').write_text('PULSE_PORT=8091\n')
    assert upgrade.main(['--backup-dir', str(tmp_path / 'backup')]) == 1
    assert not any(c[-1] in ('stop', 'start-offline') for c in calls)
