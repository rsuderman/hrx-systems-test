"""Restore an SDK acquisition plan from frozen offline preparation inputs."""
from inspect import signature


def restore(document, selection, saved, *, offline, lock_text):
    from loom.model.fetch import FetchPlan
    from loom.model.fetch_source import Source
    from loom.model.lock import FileIdentity, ModelLock

    arguments = dict(document=document, selection=selection,
        sources={name: Source(**source) for name, source in saved['sources'].items()},
        identities={name: FileIdentity(**identity) for name, identity in saved['identities'].items()},
        origins=saved['origins'], offline=offline, lock_mode=saved['lock_mode'],
        lock_scope=saved['lock_scope'], update_path=None)
    # Release SDKs have no previous_lock field. Newer SDKs need the original
    # parsed lock to preserve authored source annotations when relocating it.
    # Detect the actual API: different builds share the same package version.
    if 'previous_lock' in signature(FetchPlan).parameters:
        arguments['previous_lock'] = ModelLock.parse(lock_text)
    return FetchPlan(**arguments)
