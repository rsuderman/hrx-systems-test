"""Trusted code roots, command vectors, source mounts and provenance paths.

This module performs no configuration, network, Docker or model operations.
"""
from pathlib import Path, PurePosixPath
import sys

PYTHON_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = PYTHON_ROOT / 'hrx_systems'
REPO_ROOT = PYTHON_ROOT.parent
LAUNCHER_RELATIVE = 'python/run.py'
MIGRATION_RELATIVE = 'tools/migrations/upgrade-pipelines-0.1.0-to-0.2.0.py'
LAYOUT_VERSION = 2


def repository_path(relative):
    path = PurePosixPath(relative)
    if path.is_absolute() or '..' in path.parts or '\\' in str(relative) or str(path) != str(relative):
        raise ValueError('repository code paths must be normalized and contained')
    return REPO_ROOT / path


def launcher_path(*, container=False):
    return Path('/source/python/run.py') if container else repository_path(LAUNCHER_RELATIVE)


def migration_path(*, container=False):
    return Path('/source') / MIGRATION_RELATIVE if container else repository_path(MIGRATION_RELATIVE)


def command(role, arguments=(), *, interpreter=None, container=False, unbuffered=False):
    from .cli import module_for
    module_for(role)
    if not isinstance(arguments, (tuple, list)) or any(not isinstance(value, (str, Path)) for value in arguments):
        raise ValueError('command arguments must be an explicit argument vector')
    return [str(interpreter or sys.executable), *(['-u'] if unbuffered else []), '-I',
            str(launcher_path(container=container)), role, *map(str, arguments)]


def source_mounts():
    """Only implementation Python and the portable migration tool enter containers."""
    return [(str(PYTHON_ROOT), '/source/python'),
            (str(REPO_ROOT / 'tools/migrations'), '/source/tools/migrations')]


def mount_arguments():
    return [item for source, target in source_mounts()
            for item in ('--mount', f'type=bind,src={source},dst={target},readonly')]


def implementation_files():
    """Canonical repository-relative implementation closure; no tests or bytecode."""
    paths = [LAUNCHER_RELATIVE, *sorted(str(path.relative_to(REPO_ROOT))
             for path in PACKAGE_ROOT.rglob('*.py')), MIGRATION_RELATIVE]
    for name in paths:
        if not repository_path(name).resolve().is_relative_to(REPO_ROOT):
            raise ValueError('implementation source escapes the trusted repository')
    return paths
