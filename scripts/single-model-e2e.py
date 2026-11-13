#!/usr/bin/env python3
"""Legacy CLI for the historical test fixture; no production dispatcher role."""
from pathlib import Path
import runpy
import sys

if __name__ == '__main__':
    fixture = Path(__file__).resolve().parents[1] / 'tests/support/single_model_e2e.py'
    raise SystemExit(runpy.run_path(str(fixture))['main'](sys.argv[1:]))
