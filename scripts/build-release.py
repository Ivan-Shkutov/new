#!/usr/bin/env python3
"""Package tracked source plus Linux amd64 wheels, excluding all previous releases.

Prepare wheels first using pip download --require-hashes --only-binary=:all:
-r requirements.txt -d wheelhouse. Stage new source files before packaging.
"""
import hashlib
import argparse
import io
import json
import sys
from pathlib import Path
import re
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--include-image', action='store_true', help='Include the prebuilt Docker image for installation without a registry')
    args = parser.parse_args()
    version = re.search(r"__version__ = '([^']+)'", (ROOT / 'pulse_app/__init__.py').read_text())[1]
    prefix = f'pulse-clusters-{version}'
    tracked = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
    sources = [ROOT / name for name in tracked if name and not name.startswith(('releases/', 'runtime/', 'wheelhouse/'))]
    wheels = sorted((ROOT / 'wheelhouse').glob('*.whl'))
    if not wheels:
        raise SystemExit('Download the hashed wheelhouse before packaging')
    files = sources + wheels
    if args.include_image:
        image = ROOT / 'image' / f'{prefix}.tar.gz'
        if not image.is_file():
            raise SystemExit('Export the verified Docker image to ' + str(image))
        files.append(image)
    if any(not p.is_file() or p.is_symlink() for p in files):
        raise SystemExit('Every release input must be a regular file')
    output = ROOT / 'releases' / f'{prefix}-complete.tar.gz'
    output.parent.mkdir(exist_ok=True)
    manifest = ''.join(f'{digest(p)}  {p.relative_to(ROOT)}\n' for p in files).encode()
    sys.path.insert(0,str(ROOT))
    from pulse_app.build import identity
    release_info={k:v for k,v in identity().items() if k!='python'}
    release_json=(json.dumps(release_info,indent=2)+'\n').encode()
    manifest+=(hashlib.sha256(release_json).hexdigest()+'  RELEASE.json\n').encode()
    with tarfile.open(output, 'w:gz') as archive:
        for path in files:
            archive.add(path, arcname=f'{prefix}/{path.relative_to(ROOT)}', recursive=False)
        release_entry=tarfile.TarInfo(f'{prefix}/RELEASE.json');release_entry.size=len(release_json);release_entry.mode=0o644
        archive.addfile(release_entry,io.BytesIO(release_json))
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
