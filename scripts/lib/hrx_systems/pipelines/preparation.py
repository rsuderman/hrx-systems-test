"""SDK-native pipeline preparation; host orchestration supplies its services."""
from dataclasses import asdict, replace
import re


class NativePreparation:
    def __init__(self, *, seed, assets, cache, policy, inventory, contained, read_json, write_json, digest):
        self.seed, self.assets, self.cache = seed, assets, cache
        self.policy, self.inventory = policy, inventory
        self.contained, self.read_json, self.write_json, self.digest = contained, read_json, write_json, digest

    @staticmethod
    def selection_identity(baseline):
        return {key: baseline[key] for key in ('repository', 'publication_revision', 'model_id', 'preset', 'variant', 'programs')}

    def indexed_cache(self, selection, seed, cache_root, work, verifier=None, code=None):
        """Compose no-fallback staged pipeline reads with immutable indexed assets."""
        from loom.model.fetch_storage import AcquiredFile, ContentCache
        from loom.model.lock import file_key
        entries = {item['installation_path']: item for item in seed['files']}
        wanted = {name for name, file in selection.files.items() if not self.seed.code_file(file)}
        if len(entries) != len(seed['files']) or set(entries) != wanted:
            raise ValueError('seed manifest asset closure does not match selected card')
        code_origins = {file_key(file.origin) for file in selection.files.values() if self.seed.code_file(file)}
        by_origin = {}
        for name in wanted:
            file = selection.files[name]
            if file.transform is not None:
                raise ValueError('initial prepared baseline requires untransformed assets')
            key = file_key(file.origin)
            if key in code_origins:
                raise ValueError('mixed-origin code/asset selection needs explicit seed routing')
            item = entries[name]
            if sorted(file.roles) != sorted(item['roles']) or not item.get('verified'):
                raise ValueError(f'seed roles or verification do not match: {name}')
            if file_key(item['origin']) != key:
                raise ValueError(f'seed origin does not match: {name}')
            previous = by_origin.setdefault(key, item)
            if previous['sha256'] != item['sha256'] or previous['size'] != item['size']:
                raise ValueError('conflicting indexed identities for one asset source')
        if code is None:
            entry = self.cache.read_entry(cache_root / 'pipelines', seed['selection'],
                                                      seed['card_sha256'], seed['lock_sha256'])
            code = self.cache.native_cache(entry, work / 'pipeline-staged')
        verifier = verifier or self.assets.Verifier()

        contain = self.contained

        class IndexedCache(ContentCache):
            def __init__(self):
                super().__init__(code.root, huggingface=code.huggingface)
                self.verifier = verifier

            def indexed(self, source, identity):
                item = by_origin.get(file_key(source.origin))
                if item is None:
                    return None
                if (identity is None or identity.sha256 != item['sha256'] or
                        identity.size != item['size'] or source.resolved != item['resolved']):
                    raise ValueError('seed identity/revision disagrees with selected lock')
                if item['cache_root'] not in ('huggingface', 'loom-assets'):
                    raise ValueError('invalid model cache root')
                return contain(cache_root / item['cache_root'], item['cache_path'])

            def read_document(self, location, *, offline):
                return code.read_document(location, offline=offline)

            def prepare_source(self, source, identity, *, offline):
                path = self.indexed(source, identity)
                if path is not None:
                    return replace(source, transport={'kind': 'local', 'path': str(path)})
                return code.prepare_source(source, identity, offline=offline)

            def acquire(self, source, identity, *, offline, progress=None):
                path = self.indexed(source, identity)
                if path is None:
                    return code.acquire(source, identity, offline=offline, progress=progress)
                self.verifier.verify(path, identity.size, identity.sha256)
                return AcquiredFile(path, identity)

        return IndexedCache()


    def online(self, baseline, seed, cache_root, work, *, location=None, policy=None):
        from loom.model.fetch import plan_fetch
        from loom.model.fetch_source import Document
        if seed.get('schema_version') != 1 or seed['selection'] != self.selection_identity(baseline):
            raise ValueError('seed selection does not match baseline')
        repository, revision = baseline['repository'], baseline['publication_revision']
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository) or not re.fullmatch(r'[0-9a-f]{40}', revision):
            raise ValueError('pipeline publication must identify one repository and immutable commit')
        location = location or f'https://huggingface.co/{repository}/resolve/{revision}/model.json'
        entry = self.cache.read_entry(cache_root / 'pipelines', seed['selection'],
                                                  seed['card_sha256'], seed['lock_sha256'])
        if baseline.get('pipeline_cache', {}).get('manifest_sha256') not in (None, entry.manifest_sha256):
            raise self.cache.CacheError('seed_mismatch', 'pipeline cache manifest changed after host verification')
        documents = self.cache.native_cache(entry, work / 'pipeline-staged')
        document = documents.read_document(location, offline=True)
        lock = documents.read_document(location + '.lock', offline=True)
        (work / 'model.json').write_text(document.text)
        (work / 'model.json.lock').write_text(lock.text)
        if self.digest(work / 'model.json') != seed['card_sha256'] or self.digest(work / 'model.json.lock') != seed['lock_sha256']:
            raise ValueError('published card/lock disagree with seeded selection')
        selection = self.seed.selection_for(baseline, document.text)
        if baseline.get('selection_mode') == 'cached':
            baseline.update(self.inventory.workload(baseline, selection))
        if policy is not None:
            baseline = self.policy.profile(baseline, policy)
        cache = self.indexed_cache(selection, seed, cache_root, work, code=documents)
        plan = plan_fetch(Document(location, document.text), selection, cache,
                          lock_location=location + '.lock')
        for name, file in selection.files.items():
            if not self.seed.code_file(file):
                continue
            if file.transform is not None:
                raise ValueError('initial prepared baseline requires untransformed programs')
            item = cache.acquire(plan.sources[name], plan.identities.get(name), offline=True)
            plan.sources[name] = replace(plan.sources[name], transport={'kind': 'local', 'path': str(item.path)})
        self.write_json(work / 'sources.json', {'document': asdict(plan.document),
            'sources': {name: asdict(source) for name, source in plan.sources.items()},
            'identities': {name: asdict(identity) for name, identity in plan.identities.items()},
            'origins': plan.origins, 'lock_mode': plan.lock_mode, 'lock_scope': plan.lock_scope})
        return baseline


    def offline(self, baseline, seed, cache_root, work, destination, entry_id, sdk_identity, verifier=None, policy=None):
        from loom.model.fetch import FetchPlan, install
        from loom.model.fetch_source import Document, Source
        from loom.model.lock import FileIdentity
        saved = self.read_json(work / 'sources.json')
        document = Document(**saved['document'])
        selection = self.seed.selection_for(baseline, document.text)
        cache = self.indexed_cache(selection, seed, cache_root, work, verifier)
        plan = FetchPlan(document, selection,
            {name: Source(**source) for name, source in saved['sources'].items()},
            {name: FileIdentity(**identity) for name, identity in saved['identities'].items()},
            saved['origins'], True, saved['lock_mode'], saved['lock_scope'], None)
        # Local asset transports make native sharing probe the actual immutable files,
        # avoiding provider metadata writes or a second asset store.
        for name, source in plan.sources.items():
            if not self.seed.code_file(selection.files[name]):
                path = cache.indexed(source, plan.identities.get(name))
                plan.sources[name] = replace(source, transport={'kind': 'local', 'path': str(path)})
        install(plan, cache, destination / 'model')
        # Native installation may share source through a staging symlink. Materialize
        # only code so no execution input depends on ephemeral work or host cache paths.
        for name, file in selection.files.items():
            installed = destination / 'model' / name
            if self.seed.code_file(file) and installed.is_symlink():
                data = installed.read_bytes()
                installed.unlink()
                installed.write_bytes(data)
        upgrade = None
        if policy is not None:
            upgrade = self.policy.seal_upgrade(destination, work, seed, policy, entry_id)
            selected = self.seed.selection_for(baseline, (destination / 'model/model.json').read_text())
            baseline['effective_execution'] = dict(selected.settings['execution'])
        assets, programs = [], []
        index = {item['installation_path']: item for item in seed['files']}
        for name, file in selection.files.items():
            installed = destination / 'model' / name
            if not installed.is_file():
                raise ValueError(f'installed file is missing from final mount: {name}')
            if self.seed.code_file(file):
                programs.append({'path': name, 'sha256': self.digest(installed)})
            else:
                item = index[name]
                path = self.assets.asset_path(cache_root, item)
                receipt = cache.verifier.verify(path, item['size'], item['sha256'])
                installed_receipt = (receipt if installed.samefile(path) else
                                     cache.verifier.verify(installed, item['size'], item['sha256']))
                assets.append({**item, 'verification': receipt, 'installed_verification': installed_receipt})
        manifest = {'schema_version': 1, 'selection': self.selection_identity(baseline),
            'model_id': baseline['model_id'], 'model_card_path': f'/inputs/{entry_id}/model/model.json',
            'card_sha256': seed['card_sha256'], 'lock_sha256': seed['lock_sha256'],
            **({'pipeline_cache': baseline['pipeline_cache']} if 'pipeline_cache' in baseline else {}),
            'installed_card_sha256': self.digest(destination / 'model/model.json'),
            'programs': programs, 'assets': assets, 'sdk_identity': sdk_identity,
            'effective_execution': baseline.get('effective_execution', baseline['published_execution']),
            'smoke_request': baseline['smoke_request'], 'smoke_expected_text': baseline['smoke_expected_text'],
            'test_kind': baseline.get('test_kind', 'text_smoke'),
            'profile_identity': baseline.get('profile_identity', 'explicit-baseline'),
            'service_kind': baseline.get('service_kind', 'text'),
            'asset_phase_network': 'disabled', 'asset_verification': dict(cache.verifier.stats), 'model_executed': False}
        if upgrade is not None:
            manifest.update(published_execution=baseline['published_execution'],
                effective_test_profile=baseline['effective_test_profile'], pipeline_upgrade=upgrade,
                evidence=[upgrade['report_path'], *[item['path'] for item in upgrade['publication']]])
        self.write_json(destination / 'prepared-inputs.json', manifest)
        return manifest
