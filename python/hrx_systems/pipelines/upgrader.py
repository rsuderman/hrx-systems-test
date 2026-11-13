"""Fixed adapter to the portable versioned migration; no general file loading."""
import importlib.util
from functools import lru_cache
from ..layout import migration_path


@lru_cache(maxsize=1)
def load():
    path = migration_path()
    spec = importlib.util.spec_from_file_location('hrx_systems_portable_migration_0_1_to_0_2', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
