#!/usr/bin/env python3
"""Run current compact overview and contextual-analysis browser regression."""
import subprocess
import sys
from pathlib import Path
if __name__=='__main__':
    sys.exit(subprocess.call([sys.executable,str(Path(__file__).with_name('compact-ui-smoke.py')),*sys.argv[1:]]))
