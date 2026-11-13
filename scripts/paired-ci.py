#!/usr/bin/env python3
"""Legacy paired-CI CLI alias; implementation lives under python/hrx_systems."""
from pathlib import Path
import runpy
import sys

if __name__ == '__main__':
    launcher = Path(__file__).resolve().parents[1] / 'python/run.py'
    raise SystemExit(runpy.run_path(str(launcher))['main'](['paired-ci', *sys.argv[1:]]))
