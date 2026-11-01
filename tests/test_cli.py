import fcntl
import hashlib
import importlib.machinery
import importlib.util
import io
import os
from pathlib import Path
import sqlite3
import tarfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader('pulse_cli', str(ROOT / 'pulse'))
spec = importlib.util.spec_from_loader(loader.name, loader)
cli = importlib.util.module_from_spec(spec)
loader.exec_module(cli)


def test_init_preserves_credentials_and_config(tmp_path):
    runtime = tmp_path / 'runtime'
    cli.initialize(runtime, 8090)
    password = (runtime / 'secrets/portal-password').read_text()
    key = (runtime / 'registry.key').read_bytes()
    (runtime / 'data/document.txt').write_text('keep me')
    cli.initialize(runtime, 9999)
    assert (runtime / 'secrets/portal-password').read_text() == password
    assert (runtime / 'registry.key').read_bytes() == key
    assert cli.load_env(runtime)['PULSE_PORT'] == '8090'
    assert (runtime / 'data/document.txt').read_text() == 'keep me'
    assert len(password.strip()) >= 32
    assert (runtime / 'secrets/portal-password').stat().st_mode & 0o077 == 0


def test_backup_restore_roundtrip(tmp_path):
    runtime = tmp_path / 'runtime'
    cli.initialize(runtime)
    connection = sqlite3.connect(runtime / 'pulse.sqlite3')
    connection.execute('CREATE TABLE notes (text TEXT)')
    connection.execute('INSERT INTO notes VALUES (?)', ('original',))
    connection.commit()
    connection.close()
    (runtime / 'data/document.txt').write_text('original document')
    backup = cli.make_backup(runtime, tmp_path / 'backups')
    original_key = (runtime / 'registry.key').read_bytes()
    (runtime / 'data/document.txt').write_text('changed')
    preserved = cli.restore_backup(runtime, backup)
    assert preserved.is_dir()
    assert (preserved / 'data/document.txt').read_text() == 'changed'
    assert (runtime / 'data/document.txt').read_text() == 'original document'
    assert (runtime / 'registry.key').read_bytes() == original_key
    with sqlite3.connect(runtime / 'pulse.sqlite3') as conn:
        assert conn.execute('SELECT text FROM notes').fetchall() == [('original',)]


@pytest.mark.parametrize('name,kind', [('../escape', 'file'), ('/tmp/escape', 'file'), ('runtime/link', 'symlink'), ('runtime/hardlink', 'hardlink'), ('runtime/fifo', 'fifo')])
def test_restore_rejects_unsafe_members(tmp_path, name, kind):
    runtime = tmp_path / 'runtime'
    cli.initialize(runtime)
    archive = tmp_path / 'attack.tar.gz'
    with tarfile.open(archive, 'w:gz') as stream:
        member = tarfile.TarInfo(name)
        if kind == 'symlink':
            member.type = tarfile.SYMTYPE
            member.linkname = '/etc/passwd'
        elif kind == 'hardlink':
            member.type = tarfile.LNKTYPE
            member.linkname = '/etc/passwd'
        elif kind == 'fifo':
            member.type = tarfile.FIFOTYPE
        else:
            member.size = 3
        stream.addfile(member, io.BytesIO(b'bad') if kind == 'file' else None)
    Path(str(archive) + '.sha256').write_text(hashlib.sha256(archive.read_bytes()).hexdigest())
    password = (runtime / 'secrets/portal-password').read_text()
    with pytest.raises(ValueError):
        cli.restore_backup(runtime, archive)
    assert (runtime / 'secrets/portal-password').read_text() == password
    assert not (tmp_path / 'escape').exists()


def test_restore_checks_checksum_before_replacement(tmp_path):
    runtime = tmp_path / 'runtime'
    cli.initialize(runtime)
    backup = cli.make_backup(runtime, tmp_path / 'backups')
    backup.write_bytes(backup.read_bytes() + b'corrupted')
    with pytest.raises(ValueError, match='SHA256'):
        cli.restore_backup(runtime, backup)
    assert runtime.is_dir()


def test_backup_refuses_active_controller(tmp_path):
    runtime = tmp_path / 'runtime'
    cli.initialize(runtime)
    with (runtime / 'controller.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match='running process'):
            cli.make_backup(runtime, tmp_path / 'backups')


def test_backup_rejects_links_and_nested_destination(tmp_path):
    runtime = tmp_path / 'runtime'
    cli.initialize(runtime)
    with pytest.raises(ValueError, match='outside'):
        cli.make_backup(runtime, runtime / 'backups')
    (runtime / 'leak').symlink_to('/etc/passwd')
    with pytest.raises(ValueError, match='links'):
        cli.make_backup(runtime, tmp_path / 'backups')


def test_restore_rejects_corrupted_database(tmp_path):
    runtime = tmp_path / 'runtime'
    cli.initialize(runtime)
    (runtime / 'pulse.sqlite3').write_bytes(b'not a database')
    with pytest.raises(sqlite3.DatabaseError):
        cli.make_backup(runtime, tmp_path / 'backups')


def test_compose_command_keeps_project_scoped(tmp_path):
    runtime = tmp_path / 'runtime'
    cli.initialize(runtime, 8091)
    command = cli.compose_command(runtime)
    assert 'pulse-clusters-8091' in command
    assert str(ROOT / 'compose.yaml') in command


def test_init_never_replaces_lost_key_for_existing_database(tmp_path):
    runtime = tmp_path / 'runtime'
    cli.initialize(runtime)
    (runtime / 'pulse.sqlite3').write_bytes(b'existing encrypted installation')
    (runtime / 'registry.key').unlink()
    with pytest.raises(ValueError, match='original key'):
        cli.initialize(runtime)
    assert not (runtime / 'registry.key').exists()


@pytest.mark.parametrize('running_version,running_build',[('0.13.0','old'),('0.14.0','0000000000000000')])
def test_check_rejects_wrong_running_release_or_code(tmp_path,monkeypatch,running_version,running_build):
    import json
    runtime=tmp_path/'runtime';cli.initialize(runtime)
    def reply(*args,**kwargs):return io.BytesIO(json.dumps({'status':'ok','version':running_version,'build_id':running_build}).encode())
    monkeypatch.setattr(cli.urllib.request.OpenerDirector,'open',reply)
    with pytest.raises(ValueError,match='другая сборка'):cli.health_check(runtime)
