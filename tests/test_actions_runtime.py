import argparse
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock
import urllib.error
import zipfile

import runpy
runpy.run_path(str(Path(__file__).resolve().parents[1] / "python/run.py"))["bootstrap"]()
from hrx_systems.sdk import actions as A
HEAD, OLD = 'a' * 40, 'b' * 40
NOW = '2026-11-11T12:00:00Z'
ENV = {'python_version': '3.12', 'python_full_version': '3.12.5', 'sys_platform': 'linux',
       'platform_machine': 'x86_64', 'glibc_version': '2.39', 'os_name': 'posix',
       'platform_system': 'Linux', 'implementation_name': 'cpython', 'implementation_version': '3.12.5',
       'platform_python_implementation': 'CPython'}
SELECTOR = {'kind': 'github_actions', 'repository': A.REPOSITORY, 'branch': 'main',
            'mode': 'latest-successful', 'artifact_name': A.ARTIFACT_NAME}


def run(number, sha=OLD, **changes):
    return {'id': number, 'run_attempt': 1, 'head_sha': sha, 'head_branch': 'main',
        'event': 'push', 'status': 'completed', 'conclusion': 'success', 'path': A.WORKFLOWS[0],
        'repository': {'full_name': A.REPOSITORY}, 'head_repository': {'full_name': A.REPOSITORY},
        'created_at': '2026-11-10T10:00:00Z', 'run_started_at': '2026-11-10T10:00:00Z',
        'updated_at': '2026-11-10T11:00:00Z', **changes}


def artifact(owner, **changes):
    return {'id': owner['id'] * 10, 'name': A.ARTIFACT_NAME, 'expired': False,
        'created_at': '2026-11-10T10:30:00Z', 'expires_at': '2026-11-20T10:30:00Z',
        'size_in_bytes': 100, 'digest': 'sha256:' + 'c' * 64,
        'workflow_run': {'id': owner['id'], 'head_sha': owner['head_sha'], 'head_branch': 'main'}, **changes}


class Client:
    def __init__(self, runs=(), artifacts=None, compare='ahead'):
        self.runs = list(runs)
        self.artifacts = artifacts if artifacts is not None else {r['id']: [artifact(r)] for r in self.runs}
        self.compare = compare
        self.calls = []
        self.body = None

    def json(self, path):
        self.calls.append(path)
        if path.endswith('/commits/main'): return {'sha': HEAD}
        if '/compare/' in path: return {'status': self.compare, 'merge_base_commit': {'sha': OLD}}
        if '/workflows/' in path: return {'workflow_runs': self.runs if A.WORKFLOWS[0].rsplit('/', 1)[1] in path else []}
        if '/artifacts?' in path:
            return {'artifacts': self.artifacts.get(int(path.split('/runs/')[1].split('/')[0]), [])}
        if '/runs/' in path: return next(r for r in self.runs if str(r['id']) == path.rsplit('/', 1)[1])
        raise AssertionError(path)

    def download(self, ident, destination, expected_size):
        self.calls.append(('download', ident))
        Path(destination).write_bytes(self.body)


class ResolutionTests(unittest.TestCase):
    def resolve(self, client, selector=SELECTOR):
        return A.resolve_source(selector, client=client, now=NOW)

    def test_exact_head_preferred_even_if_older_than_other_build(self):
        exact = run(1, HEAD, created_at='2026-11-09T10:00:00Z', run_started_at='2026-11-09T10:00:00Z')
        c = Client([run(2), exact])
        value = self.resolve(c)
        self.assertEqual(value['run_id'], 1)
        self.assertEqual(value['relation_to_head'], 'equal')
        self.assertEqual(sum('/commits/main' in p for p in c.calls), 1)
        self.assertFalse(value['artifact']['digest_verified'])

    def test_behind_head_source_is_explicit_and_verified(self):
        c = Client([run(2)])
        value = self.resolve(c)
        self.assertEqual((value['requested_head_sha'], value['tested_sha']), (HEAD, OLD))
        self.assertEqual(value['relation_to_head'], 'behind')
        self.assertEqual(value['build_age_seconds'], 90000)
        self.assertTrue(any('/compare/' in p for p in c.calls))

    def test_skip_expired_or_missing_and_select_available_build(self):
        c = Client([run(3), run(2), run(1)])
        c.artifacts[3] = [artifact(run(3), expired=True)]
        c.artifacts[2] = []
        self.assertEqual(self.resolve(c)['run_id'], 1)

    def test_reject_topic_pr_fork_failed_and_future_runs(self):
        invalid = [run(1, head_branch='topic'), run(2, event='pull_request'),
                   run(3, head_repository={'full_name': 'fork/hrx'}), run(4, conclusion='failure'),
                   run(5, updated_at='2026-11-12T00:00:00Z'), run(6, path='unknown.yml')]
        with self.assertRaisesRegex(A.ActionsError, 'No successful'):
            self.resolve(Client(invalid))

    def test_artifact_ownership_digest_attempt_and_ambiguity(self):
        for change, message in [({'workflow_run': {'id': 99}}, 'belong'), ({'digest': None}, 'digest'),
                                ({'created_at': '2026-11-09T00:00:00Z'}, 'attempt')]:
            with self.subTest(change=change):
                c = Client([run(1)], {1: [artifact(run(1), **change)]})
                with self.assertRaisesRegex(A.ActionsError, message): self.resolve(c)
        with self.assertRaisesRegex(A.ActionsError, 'ambiguous'):
            self.resolve(Client([run(1)], {1: [artifact(run(1)), artifact(run(1), id=11)]}))

    def test_diverged_or_unverified_source_rejected(self):
        with self.assertRaisesRegex(A.ActionsError, 'ancestor'):
            self.resolve(Client([run(1)], compare='diverged'))

    def test_replay_never_replaces_missing_artifact_or_attempt(self):
        selector = {**SELECTOR, 'mode': 'replay', 'run_id': 1, 'run_attempt': 1,
                    'artifact_id': 10, 'wheel_sha256': 'd' * 64}
        self.assertEqual(self.resolve(Client([run(1)]), selector)['expected_wheel_sha256'], 'd' * 64)
        with self.assertRaisesRegex(A.ActionsError, 'attempt'):
            self.resolve(Client([run(1, run_attempt=2)]), selector)
        with self.assertRaisesRegex(A.ActionsError, 'unavailable'):
            self.resolve(Client([run(1)], {1: []}), selector)

    def test_api_failure_is_not_empty_inventory(self):
        class Broken(Client):
            def json(self, path): raise A.ActionsError('permission failure', 'authentication')
        with self.assertRaisesRegex(A.ActionsError, 'permission'):
            self.resolve(Broken())

    def test_pagination_is_consumed_and_bounded(self):
        class Pages(Client):
            def json(self, path):
                if '/workflows/' in path and A.WORKFLOWS[0].rsplit('/', 1)[1] in path:
                    return {'workflow_runs': [run(i) for i in range(1, 101)] if path.endswith('&page=1') else [run(101)]}
                return super().json(path)
        c = Pages([run(101)])
        self.assertEqual(self.resolve(c)['run_id'], 101)

    def test_source_controls_validate_replay_and_release(self):
        p = argparse.ArgumentParser(); A.add_source_arguments(p)
        release = {'kind': 'release', 'release': 'v0.1.0'}
        self.assertEqual(A.selector_from_args(p.parse_args([]), release), release)
        self.assertEqual(A.selector_from_args(p.parse_args(['--runtime-source', 'actions-main']))['mode'], 'latest-successful')
        with self.assertRaises(A.ActionsError):
            A.selector_from_args(p.parse_args(['--runtime-run-id', '1']))
        with self.assertRaises(A.ActionsError):
            A.selector_from_args(p.parse_args(['--runtime-source', 'actions-replay']))


def wheel_bytes(requires=(), name='hrx_loom', version='0.2.0', requires_python='>=3.12', tag='py3-none-manylinux_2_39_x86_64'):
    content = io.BytesIO()
    metadata = f'Metadata-Version: 2.1\nName: {name}\nVersion: {version}\nRequires-Python: {requires_python}\n'
    metadata += ''.join('Requires-Dist: ' + value + '\n' for value in requires)
    with zipfile.ZipFile(content, 'w') as z:
        z.writestr(f'{name}-{version}.dist-info/METADATA', metadata)
        z.writestr(f'{name}-{version}.dist-info/WHEEL', 'Wheel-Version: 1.0\nTag: ' + tag + '\n')
    return content.getvalue()


class AcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.wheel_name = 'hrx_loom-0.2.0-py3-none-manylinux_2_39_x86_64.whl'
        self.lock = {'schema_version': 1, 'python': '3.12', 'platform': 'linux_x86_64',
                     'wheels': [{'name': 'dep', 'version': '2.0'}]}

    def acquire(self, body=None, source_changes=None, **kwargs):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as z: z.writestr(self.wheel_name, body if body is not None else wheel_bytes(['dep>=1']))
        c = Client([run(1)]); c.body = archive.getvalue()
        source = A.resolve_source(SELECTOR, client=c, now=NOW)
        source['artifact'].update(size_bytes=len(c.body), upstream_digest='sha256:' + hashlib.sha256(c.body).hexdigest())
        if source_changes: source.update(source_changes)
        return A.acquire_wheel(source, self.root / 'hrx', dependency_lock=self.lock, environment=ENV, client=c, **kwargs)

    def test_verified_single_wheel_has_full_identity_and_no_archive_left(self):
        result = self.acquire()
        self.assertTrue(result['source']['artifact']['digest_verified'])
        self.assertEqual(result['wheel']['name'], 'hrx_loom')
        self.assertEqual(result['wheel']['version'], '0.2.0')
        self.assertEqual(Path(result['staged_path']).name, self.wheel_name)
        self.assertEqual(list((self.root / 'hrx').iterdir()), [Path(result['staged_path'])])
        self.assertIn('pending_offline_install', result['compatibility_status'])

    def test_digest_mismatch_is_not_published(self):
        c = Client([run(1)]); c.body = b'bad ZIP'
        source = A.resolve_source(SELECTOR, client=c, now=NOW)
        with self.assertRaisesRegex(A.ActionsError, 'SHA256'):
            A.acquire_wheel(source, self.root / 'hrx', dependency_lock=self.lock, environment=ENV, client=c)
        self.assertEqual(list((self.root / 'hrx').iterdir()), [])

    def test_replay_wheel_hash_is_checked(self):
        with self.assertRaisesRegex(A.ActionsError, 'Replay wheel'):
            self.acquire(source_changes={'expected_wheel_sha256': 'e' * 64})
        self.assertEqual(list((self.root / 'hrx').iterdir()), [])

    def test_python_glibc_metadata_and_locked_requirement_failures(self):
        for content, reason in [(wheel_bytes(requires_python='>=3.14'), 'Requires-Python'),
                                (wheel_bytes(['dep>=3']), 'dependency_incompatible'),
                                (wheel_bytes(['missing>=1']), 'dependency_incompatible'),
                                (wheel_bytes(name='other'), 'name/version')]:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as other:
                oldroot = self.root; self.root = Path(other)
                with self.assertRaisesRegex(A.ActionsError, reason): self.acquire(content)
                self.root = oldroot
        path = self.root / self.wheel_name; path.write_bytes(wheel_bytes())
        with self.assertRaisesRegex(A.ActionsError, 'glibc'):
            A.wheel_metadata(path, environment={**ENV, 'glibc_version': '2.38'})

    def test_markers_and_extra_requirements_are_not_plain_string_comparisons(self):
        status = A.validate_dependencies({'name': 'hrx-loom', 'requires_dist': [
            'dep>=1; python_version >= "3.12"', 'missing>=1; sys_platform == "win32"',
            'missing>=1; extra == "audio"']}, self.lock, environment=ENV)
        self.assertIn('compatible', status)
        with self.assertRaisesRegex(A.ActionsError, 'requires dep'):
            A.validate_dependencies({'name': 'hrx-loom', 'requires_dist': ['dep @ https://example.invalid/dep.whl']}, self.lock, environment=ENV)

    def test_transitive_dependency_metadata_is_checked(self):
        dependencies = self.root / 'python'; dependencies.mkdir()
        path = dependencies / 'dep.whl'
        data = wheel_bytes(['missing>=1'], name='dep', version='2.0')
        path.write_bytes(data)
        self.lock['wheels'][0].update(filename='dep.whl', sha256=hashlib.sha256(data).hexdigest())
        with self.assertRaisesRegex(A.ActionsError, 'missing'):
            A.validate_dependencies({'name': 'hrx-loom', 'requires_dist': ['dep']}, self.lock, environment=ENV, dependency_directory=dependencies)

    def test_unsafe_archive_and_ambiguous_payloads_rejected(self):
        for names in [('../escape.whl',), (self.wheel_name, 'second.whl'), ('tokenizer.json',)]:
            with self.subTest(names=names), tempfile.TemporaryDirectory() as temporary:
                c = Client([run(1)]); output = io.BytesIO()
                with zipfile.ZipFile(output, 'w') as z:
                    for name in names: z.writestr(name, b'fixture')
                c.body = output.getvalue()
                source = A.resolve_source(SELECTOR, client=c, now=NOW)
                source['artifact'].update(size_bytes=len(c.body), upstream_digest='sha256:' + hashlib.sha256(c.body).hexdigest())
                with self.assertRaises(A.ActionsError):
                    A.acquire_wheel(source, Path(temporary) / 'hrx', dependency_lock=self.lock, environment=ENV, client=c)

    def test_symlink_member_rejected(self):
        member = zipfile.ZipInfo('alias.whl'); member.external_attr = (stat.S_IFLNK | 0o777) << 16
        with self.assertRaisesRegex(A.ActionsError, 'symlink'): A._member(member)

    def test_api_compare_path_is_valid_and_redirect_does_not_forward_credentials(self):
        client = A.GitHubClient(token='fixture-secret')
        request = client._request(f'/repos/{A.REPOSITORY}/compare/{OLD}...{HEAD}')
        self.assertIn('compare/', request.full_url)
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.HTTPError('https://api.github.com/controlled', 302, 'redirect',
            {'Location': 'https://fixture.blob.core.windows.net/zip?signature=private'}, None)
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.side_effect = [b'abc', b'']
        with mock.patch.object(A.urllib.request, 'build_opener', return_value=opener), mock.patch.object(A.urllib.request, 'urlopen', return_value=response) as opened:
            client.download(1, self.root / 'download', 3)
        storage_request = opened.call_args.args[0]
        self.assertNotIn('Authorization', storage_request.headers)
        self.assertEqual((self.root / 'download').read_bytes(), b'abc')

    def test_optional_hf_requirement_is_active_for_the_harness(self):
        with self.assertRaisesRegex(A.ActionsError, 'requires missing'):
            A.validate_dependencies({'name': 'hrx-loom', 'requires_dist': ['missing; extra == "hf"']}, self.lock, environment=ENV)

    def test_missing_download_token_is_actionable_without_disclosure(self):
        with self.assertRaisesRegex(A.ActionsError, 'HRX_ACTIONS_TOKEN'):
            A.GitHubClient(token='').download(1, self.root / 'download', 10)


if __name__ == '__main__': unittest.main()
