"""Reproducible code identity shared by the source archive and Docker image."""
from functools import lru_cache
import hashlib
from pathlib import Path
import platform

from . import __version__


@lru_cache(maxsize=1)
def identity():
    root=Path(__file__).resolve().parents[1];package=root/'pulse_app'
    files=sorted(p for p in package.rglob('*') if p.is_file() and p.suffix in ('.py','.js','.css','.html','.md') and '__pycache__' not in p.parts)
    if (root/'requirements.txt').is_file():files.append(root/'requirements.txt')
    digest=hashlib.sha256()
    for path in sorted(files):
        digest.update(str(path.relative_to(root)).encode()+b'\0'+path.read_bytes()+b'\0')
    return {'version':__version__,'build_id':digest.hexdigest()[:16],'python':platform.python_version()}
