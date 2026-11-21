# Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
"""RC discovery, frozen provenance and serial-lane failure handling."""
import copy
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_release_runtime import R, NOW, COMMIT, release
import test_paired_ci as P
from hrx_systems.pipelines.seals import failed_selection
M = P.M


def candidate(version='0.3.0rc1', identity=10):
    row = release(tag_name='v' + version, prerelease=True, id=identity)
    asset = row['assets'][0]
    asset['name'] = asset['name'].replace('0.2.0', version)
    asset['browser_download_url'] = R._url(row['tag_name'], asset['name'])
    return row


class Client:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def json(self, path):
        self.calls.append(path)
        if path.endswith('/releases/latest'):
            return release()
        if '/releases?' in path:
            value = self.pages[int(path.rsplit('=', 1)[1]) - 1]
            if isinstance(value, Exception): raise value
            return copy.deepcopy(value)
        if '/git/ref/tags/' in path:
            return {'ref': 'refs/tags/' + path.rsplit('/', 1)[1],
                    'object': {'sha': COMMIT, 'type': 'commit'}}
        raise AssertionError(path)


class CandidateSelectionTests(unittest.TestCase):
    def test_real_client_accepts_release_arrays_but_preserves_object_endpoints(self):
        client = R.GitHubClient(token='')
        with patch.object(R.urllib.request, 'urlopen', return_value=io.BytesIO(b'[]')):
            self.assertEqual(client.json('/repos/ROCm/hrx-system/releases?per_page=100&page=1'), [])
        with patch.object(R.urllib.request, 'urlopen', return_value=io.BytesIO(b'[]')):
            with self.assertRaises(ValueError): client.json('/repos/ROCm/hrx-system/releases/latest')

    def test_numeric_order_frozen_stable_baseline_and_exact_asset(self):
        client = Client([[candidate('0.3.0rc2'), candidate('0.3.0rc10', 11)]])
        stable = R.resolve_source(client=client, now=NOW)
        frozen = R.freeze_selection(R.RC_SELECTOR, client=client, now=NOW, stable_source=stable)
        self.assertEqual(frozen['source']['version'], '0.3.0rc10')
        self.assertEqual(frozen['source']['stable_baseline']['release'], 'v0.2.0')
        self.assertEqual(sum(p.endswith('/releases/latest') for p in client.calls), 1)
        self.assertEqual(R.load_selection(frozen), frozen)
        changed = copy.deepcopy(frozen); changed['selector'] = R.SELECTOR
        with self.assertRaisesRegex(ValueError, 'channel'): R.load_selection(changed)
        changed = copy.deepcopy(frozen); changed['source']['stable_baseline']['version'] = '0.3.0'
        changed['source']['stable_baseline']['release'] = 'v0.3.0'
        with self.assertRaisesRegex(ValueError, 'newer'): R.load_selection(changed)

    def test_complete_discovery_and_no_older_fallback_for_broken_newest(self):
        newest = candidate('0.4.0rc1', 12); newest['assets'] = []
        pages = [[release()] * 99 + [candidate()], [newest]]
        with self.assertRaisesRegex(ValueError, 'missing or ambiguous'):
            R.resolve_source(R.RC_SELECTOR, client=Client(pages), now=NOW)
        with patch.object(R, 'MAX_RELEASE_PAGES', 1), self.assertRaisesRegex(ValueError, 'bounded'):
            R.resolve_source(R.RC_SELECTOR, client=Client([pages[0]]), now=NOW)

    def test_obsolete_draft_and_other_prereleases_are_not_active_rcs(self):
        old = candidate('0.2.0rc1')
        beta = candidate(); beta['tag_name'] = 'v0.4.0b1'
        draft = candidate(); draft['draft'] = True
        not_rc = candidate(); not_rc['prerelease'] = False
        with self.assertRaises(R.NoActiveCandidate):
            R.resolve_source(R.RC_SELECTOR, client=Client([[old, beta, draft, not_rc]]), now=NOW)
        with self.assertRaisesRegex(R.ReleaseError, 'permission'):
            R.resolve_source(R.RC_SELECTOR, client=Client([R.ReleaseError('permission denied')]), now=NOW)

    def test_duplicate_or_invalid_candidate_assets_fail(self):
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            R.resolve_source(R.RC_SELECTOR, client=Client([[candidate(), candidate(identity=11)]]), now=NOW)
        for mutate in ('digest', 'ambiguous', 'version', 'future'):
            row = candidate()
            if mutate == 'digest': row['assets'][0]['digest'] = None
            if mutate == 'ambiguous': row['assets'] *= 2
            if mutate == 'version': row['assets'][0]['name'] = row['assets'][0]['name'].replace('rc1', 'rc2')
            if mutate == 'future': row['published_at'] = '2026-12-01T00:00:00Z'
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                R.resolve_source(R.RC_SELECTOR, client=Client([[row]]), now=NOW)
        row = candidate()
        for suffix in ('+debug', '+asan', '+tracy'):
            row['assets'].append({**row['assets'][0], 'name': row['assets'][0]['name'].replace('rc1-', 'rc1' + suffix + '-')})
        self.assertEqual(R.resolve_source(R.RC_SELECTOR, client=Client([[row]]), now=NOW)['asset_id'], 8)

    def test_rc_failure_envelope_is_accepted_for_per_model_reporting(self):
        frozen = M.resolution_failure(R.RC_SELECTOR, ValueError('RC asset unavailable'))
        self.assertEqual(failed_selection(frozen), frozen)


class CandidatePlanTests(unittest.TestCase):
    setUp = P.PairedTests.setUp
    init = P.PairedTests.init
    raw = P.PairedTests.raw
    def plan_channels(self, mode='all', missing=False, broken_stable=False):
        calls = []
        stable = {'kind': 'release', 'release': 'v0.2.0', 'version': '0.2.0'}
        def freeze(selector, **options):
            calls.append((selector, options))
            if selector == R.SELECTOR:
                if broken_stable: raise ValueError('stable discovery failed')
                return {'schema_version': 1, 'selector': selector, 'source': stable}
            if selector == R.RC_SELECTOR:
                if mode == 'all': self.assertIs(options['stable_source'], stable)
                if missing: raise R.NoActiveCandidate('No active candidate', 'unavailable')
            return {'schema_version': 1, 'selector': selector, 'source': {'version': '0.3.0rc1'}}
        frozen = {'entries': [{'entry': {'id': 'example'}, 'selection': {'model_id': 'example'}}]}
        dest = self.root / ('planned-' + mode)
        with patch.object(M.S, 'freeze', return_value=frozen):
            doc = M.plan(self.root, dest, M.normalize('workflow_dispatch', {'runtime_source': mode}), freezer=freeze)
        self.assertEqual(M.load_plan(dest / 'ci-plan.json'), doc)
        return dest, doc, calls

    def test_three_lanes_and_common_stable_resolution(self):
        _, doc, calls = self.plan_channels()
        self.assertEqual([v['id'] for v in doc['lanes']], ['latest-release', 'latest-rc', 'latest-ci'])
        self.assertEqual(doc['rc_availability']['status'], 'selected')
        self.assertEqual(len(calls), 3)

    def test_no_rc_compacts_default_but_rc_only_reports_failure(self):
        for mode in ('all', 'release-rc'):
            with self.subTest(mode=mode):
                _, doc, _ = self.plan_channels(mode, missing=True)
                expected = ['latest-release', 'latest-ci'] if mode == 'all' else ['latest-rc']
                self.assertEqual([v['id'] for v in doc['lanes']], expected)
                self.assertEqual(doc['rc_availability']['status'], 'skipped' if mode == 'all' else 'failed')

    def test_broken_stable_does_not_resolve_a_different_baseline_for_rc(self):
        _, doc, calls = self.plan_channels(broken_stable=True)
        self.assertEqual(len(calls), 2)
        self.assertEqual([v['resolution_status'] for v in doc['lanes']], ['failed', 'failed', 'selected'])

    def successful_aggregate(self, missing=False):
        dest, doc, _ = self.plan_channels(missing=missing)
        self.path = dest / 'ci-plan.json'; self.doc = doc
        for n, lane in enumerate(doc['lanes']):
            out = self.init(lane['id']); self.raw(out)
            M.finish(out, self.stages, f'https://github.com/a/b/actions/artifacts/{n + 10}',
                     f'https://github.com/a/b/actions/artifacts/{n + 20}')
        result = M.aggregate(self.path, self.root, self.root / 'success')
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['rc_availability'], doc['rc_availability'])
        text = (self.root / 'success/summary.md').read_text()
        self.assertIn('[Download failures](https://github.com/', text)
        return result, text

    def test_successful_three_lane_aggregate_and_failure_download_links(self):
        result, text = self.successful_aggregate()
        self.assertEqual(len(result['lanes']), 3)
        self.assertIn('latest-rc', text)

    def test_no_active_rc_is_visible_and_not_counted_as_a_model_pass(self):
        result, text = self.successful_aggregate(missing=True)
        self.assertEqual(len(result['lanes']), 2)
        self.assertIn('Release candidate: **skipped**. No active candidate', text)

    def test_blocked_intermediate_lane_cannot_clear_unsafe_cleanup(self):
        M.write(self.plan / 'latest-rc.json', M.read(self.plan / 'latest-ci.json'))
        self.doc['lanes'].insert(1, {'id': 'latest-rc', 'runtime_source': 'release-rc',
            'runtime_selection': 'latest-rc.json', 'runtime_selection_sha256': M.sha(self.plan / 'latest-rc.json')})
        self.doc['rc_availability'] = {'status': 'selected', 'reason': None}
        M.write(self.path, self.doc)
        first = self.init(); self.raw(first, forced=True)
        row1 = M.finish(first, self.stages)
        second = self.init('latest-rc', safe=row1['cleanup_safe'])
        row2 = M.finish(second, self.stages, 'https://github.com/a/b/actions/artifacts/2')
        third = self.init('latest-ci', safe=row2['cleanup_safe'])
        row3 = M.finish(third, self.stages, 'https://github.com/a/b/actions/artifacts/3')
        self.assertFalse(row1['cleanup_safe'])
        self.assertEqual([row2['status'], row3['status']], ['blocked', 'blocked'])
        self.assertFalse(row2['cleanup_safe']); self.assertFalse(row3['cleanup_safe'])
        combined = M.aggregate(self.path, self.root, self.root / 'combined')
        self.assertEqual(combined['status'], 'failed')
        text = (self.root / 'combined/summary.md').read_text()
        self.assertIn('latest-rc', text); self.assertIn('Release candidate:', text)

