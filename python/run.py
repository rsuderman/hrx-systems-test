#!/usr/bin/env python3
"""Run only declared repository commands under Python isolation."""
import importlib
from pathlib import Path
import sys


def bootstrap():
    """Establish one package root without trusting CWD, environment or site hooks."""
    python_root = Path(__file__).resolve().parent
    package_root = python_root / 'hrx_systems'
    for name, module in tuple(sys.modules.items()):
        if name != 'hrx_systems' and not name.startswith('hrx_systems.'):
            continue
        origin = getattr(module, '__file__', None)
        paths = getattr(module, '__path__', ())
        if (not origin or not Path(origin).resolve().is_relative_to(package_root) or
                any(not Path(path).resolve().is_relative_to(package_root) for path in paths)):
            raise ImportError('hrx_systems belongs to a foreign package root')
    if str(python_root) not in sys.path:
        sys.path.insert(0, str(python_root))
    package = importlib.import_module('hrx_systems')
    if Path(package.__file__).resolve().parent != package_root or list(package.__path__) != [str(package_root)]:
        raise ImportError('hrx_systems must use the trusted repository package root')
    return package


def main(argv=None):
    bootstrap()
    from hrx_systems.cli import main as dispatch
    return dispatch(argv)


if __name__ == '__main__':
    raise SystemExit(main())
