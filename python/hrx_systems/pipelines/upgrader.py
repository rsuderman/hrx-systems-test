"""Fixed adapter to the portable versioned migration; no general file loading."""
import importlib.util
import json
from functools import lru_cache
from ..layout import migration_path


@lru_cache(maxsize=1)
def load():
    path = migration_path()
    spec = importlib.util.spec_from_file_location('hrx_systems_portable_migration_0_1_to_0_2', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@lru_cache(maxsize=1)
def load_proposals():
    from ..layout import repository_path, PROPOSAL_MIGRATION_RELATIVE
    path = repository_path(PROPOSAL_MIGRATION_RELATIVE)
    spec = importlib.util.spec_from_file_location('hrx_systems_portable_publication_13_to_14', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_proposal_derivative(source, output, *, legacy):
    """Compose reviewed migrations without changing either portable recipe."""
    import copy
    import shutil
    import tempfile
    from pathlib import Path
    source, output = Path(source).resolve(strict=True), Path(output).absolute()
    old, proposals = load(), load_proposals()
    adjacent = output.with_name(output.name + '.upgrade.json')
    if any(path.exists() or path.is_symlink() for path in (output, adjacent)):
        raise ValueError('pipeline derivative output already exists')
    if output.resolve().is_relative_to(source) or source.is_relative_to(output.resolve()):
        raise ValueError('input and output trees must not overlap')
    for path in source.rglob('*'):
        if path.is_file() and not path.is_symlink() and path.suffix.lower() in old.WEIGHT_SUFFIXES:
            raise ValueError('link model assets before migrating; refusing to copy ' + str(path))
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.publication-composition-', dir=output.parent) as tmp:
        intermediate = Path(tmp) / 'model'
        if legacy:
            first, first_adjacent = old.write_derivative(source, intermediate, epoch_publication_abi='control-state-13')
            first_adjacent.unlink()
        else:
            _, validated = proposals.plan(source)
            shutil.copytree(source, intermediate, symlinks=True)
            first = None
        changes, second = proposals.plan(intermediate)
        if first is None and validated['source_files'] != second['source_files']:
            raise ValueError('source changed during proposal adaptation')
        for relative, raw in changes.items():
            target = intermediate / relative
            if target.is_symlink() or not target.resolve().is_relative_to(intermediate.resolve()):
                raise ValueError('proposal adaptation path escapes source')
            target.write_bytes(raw)
        if proposals.plan(intermediate)[0]:
            raise ValueError('proposal adaptation is not idempotent')
        stages = ([first] if first else []) + [second]
        for stage in stages:
            for kind in ('source_files', 'output_files'):
                stage[kind].sort(key=lambda record: record['path'])
        report = copy.deepcopy(first or second)
        # The historical top-level implementation field remains the legacy recipe;
        # stages bind each implementation that actually transformed source.
        report.update(migration_id=old.MIGRATION_ID,
                      implementation_revision=old.IMPLEMENTATION_REVISION,
                      implementation_sha256=old.sha(Path(old.__file__).read_bytes()),
                      epoch_publication_abi='proposal-counts-14',
                      source_files=(first or second)['source_files'],
                      output_files=second['output_files'],
                      status='applied' if any(stage['status'] == 'applied' for stage in stages) else 'unchanged',
                      stages=stages)
        for stage in stages:
            stage.pop('source', None)
            stage.pop('output', None)
        report['files'] = [record for stage in stages for record in stage['files']]
        report['changed_file_count'] = len({record['path'] for record in report['files']})
        report['operation_counts'] = {}
        for stage in stages:
            for name, count in stage['operation_counts'].items():
                report['operation_counts'][name] = report['operation_counts'].get(name, 0) + count
        reserved = created_report = False
        try:
            output.mkdir()
            reserved = True
            with adjacent.open('x') as stream:
                created_report = True
                json.dump(report, stream, indent=2)
                stream.write('\n')
            intermediate.replace(output)
            reserved = False
        except BaseException:
            if created_report:
                adjacent.unlink(missing_ok=True)
            if reserved:
                output.rmdir()
            raise
    return report, adjacent


@lru_cache(maxsize=1)
def load_runner():
    from ..layout import repository_path, RUNNER_MIGRATION_RELATIVE
    spec = importlib.util.spec_from_file_location('hrx_systems_portable_runner_borrowed', repository_path(RUNNER_MIGRATION_RELATIVE))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_runner_derivative(source, output, *, legacy, target):
    """Apply borrowed ownership last and seal the entire ordered source chain."""
    import copy
    import shutil
    import tempfile
    from pathlib import Path
    source, output = Path(source).resolve(strict=True), Path(output).absolute()
    if output.resolve().is_relative_to(source) or source.is_relative_to(output.resolve()):
        raise ValueError('input and output trees must not overlap')
    adjacent = output.with_name(output.name + '.upgrade.json')
    if any(p.exists() or p.is_symlink() for p in (output, adjacent)):
        raise ValueError('pipeline derivative output already exists')
    with tempfile.TemporaryDirectory(prefix='.runner-composition-', dir=output.parent) as tmp:
        intermediate = Path(tmp) / 'model'
        if target == 'proposal-counts-14':
            base, receipt = write_proposal_derivative(source, intermediate, legacy=legacy)
            receipt.unlink()
        elif legacy:
            base, receipt = load().write_derivative(source, intermediate, epoch_publication_abi=target)
            receipt.unlink()
        else:
            _, validated = load_runner().plan(source)
            shutil.copytree(source, intermediate, symlinks=True)
            base = None
        runner = load_runner()
        changes, last = runner.plan(intermediate)
        if base is None and validated['source_files'] != last['source_files']:
            raise ValueError('source changed during runner adaptation')
        for relative, raw in changes.items():
            path = intermediate / relative
            if path.is_symlink() or not path.resolve().is_relative_to(intermediate.resolve()):
                raise ValueError('runner adaptation path escapes source')
            path.write_bytes(raw)
        if runner.plan(intermediate)[0]:
            raise ValueError('runner adaptation is not idempotent')
        stages = copy.deepcopy(base.get('stages', [base]) if base else []) + [last]
        for stage in stages:
            for key in ('source_files', 'output_files'):
                stage[key].sort(key=lambda x: x['path'])
            stage.pop('source', None)
            stage.pop('output', None)
        report = copy.deepcopy(base or last)
        report.update(migration_id=load().MIGRATION_ID,
                      implementation_revision=load().IMPLEMENTATION_REVISION,
                      implementation_sha256=load().sha(Path(load().__file__).read_bytes()),
                      runner_ownership_abi='borrowed-ptr', epoch_publication_abi=target,
                      source_files=stages[0]['source_files'], output_files=last['output_files'], stages=stages,
                      status='applied' if any(s['status'] == 'applied' for s in stages) else 'unchanged')
        report['files'] = [f for stage in stages for f in stage['files']]
        report['changed_file_count'] = len({f['path'] for f in report['files']})
        report['operation_counts'] = {}
        for stage in stages:
            for name, count in stage['operation_counts'].items():
                report['operation_counts'][name] = report['operation_counts'].get(name, 0) + count
        reserved = created_report = False
        try:
            output.mkdir()
            reserved = True
            with adjacent.open('x') as stream:
                created_report = True
                json.dump(report, stream, indent=2)
                stream.write('\n')
            intermediate.replace(output)
            reserved = False
        except BaseException:
            if created_report:
                adjacent.unlink(missing_ok=True)
            if reserved:
                output.rmdir()
            raise
    return report, adjacent
