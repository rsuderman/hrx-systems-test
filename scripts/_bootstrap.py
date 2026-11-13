"""Trusted repository imports for public commands executed with python -I."""
import importlib
import importlib.util
from pathlib import Path
import re
import sys

SCRIPTS = Path(__file__).resolve().parent
LIBRARY = SCRIPTS / 'lib'


def load_internal(relative):
    """Load only internal Python modules rooted at this checkout's library."""
    path = Path(relative)
    if (path.suffix != '.py' or path.is_absolute() or
            any(not re.fullmatch('[a-z_][a-z_0-9]*', part) for part in path.with_suffix('').parts)):
        raise ValueError('invalid internal module path')
    target = (LIBRARY / 'hrx_systems' / path).resolve(strict=True)
    if not target.is_relative_to((LIBRARY / 'hrx_systems').resolve(strict=True)):
        raise ValueError('internal module escapes repository library')
    existing = sys.modules.get('hrx_systems')
    if existing is not None and Path(existing.__file__).resolve().parent != LIBRARY / 'hrx_systems':
        raise ImportError('internal package belongs to another checkout')
    if str(LIBRARY) not in sys.path:
        sys.path.insert(0, str(LIBRARY))
    return importlib.import_module('hrx_systems.' + '.'.join(path.with_suffix('').parts))


def load_public(name):
    """Keep public helper loading explicit; no current-directory import lookup."""
    if not isinstance(name, str) or not re.fullmatch('[a-z][a-z0-9_.-]*', name):
        raise ValueError('invalid public helper name')
    target = (SCRIPTS / (name + '.py')).resolve(strict=True)
    if not target.is_relative_to(SCRIPTS.resolve(strict=True)):
        raise ValueError('public helper escapes repository scripts')
    spec = importlib.util.spec_from_file_location('hrx_public_' + name.replace('-', '_'), target)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def implementation_files():
    """Relative scripts paths included in preparation/execution provenance."""
    return ['_bootstrap.py', *sorted(str(path.relative_to(SCRIPTS))
            for path in (LIBRARY / 'hrx_systems').rglob('*.py'))]
