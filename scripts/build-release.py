#!/usr/bin/env python3
"""Package tracked source plus Linux amd64 wheels, excluding all previous releases.

Prepare wheels first using pip download --require-hashes --only-binary=:all:
-r requirements.txt -d wheelhouse. Stage new source files before packaging.
"""
import hashlib
import io
from pathlib import Path
import re
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    version = re.search(r"__version__ = '([^']+)'", (ROOT / 'pulse_app/__init__.py').read_text())[1]
    prefix = f'pulse-clusters-{version}'
    tracked = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
    sources = [ROOT / name for name in tracked if name and not name.startswith(('releases/', 'runtime/', 'wheelhouse/'))]
    wheels = sorted((ROOT / 'wheelhouse').glob('*.whl'))
    if not wheels:
        raise SystemExit('Download the hashed wheelhouse before packaging')
    files = sources + wheels
    if any(not p.is_file() or p.is_symlink() for p in files):
        raise SystemExit('Every release input must be a regular file')
    output = ROOT / 'releases' / f'{prefix}-complete.tar.gz'
    output.parent.mkdir(exist_ok=True)
    manifest = ''.join(f'{digest(p)}  {p.relative_to(ROOT)}\n' for p in files).encode()
    with tarfile.open(output, 'w:gz') as archive:
        for path in files:
            archive.add(path, arcname=f'{prefix}/{path.relative_to(ROOT)}', recursive=False)
        info = tarfile.TarInfo(f'{prefix}/MANIFEST.sha256')
        info.size, info.mode = len(manifest), 0o644
        archive.addfile(info, io.BytesIO(manifest))
    with tarfile.open(output) as archive:
        for path in files:
            content = archive.extractfile(f'{prefix}/{path.relative_to(ROOT)}').read()
            if hashlib.sha256(content).hexdigest() != digest(path):
                raise SystemExit('Release content verification failed')
    Path(str(output) + '.sha256').write_text(f'{digest(output)}  {output.name}\n')
    print(f'Verified {len(files)} files; {output.stat().st_size} bytes: {output.name}')


if __name__ == '__main__':
    main()
