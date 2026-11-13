#!/usr/bin/env python3
"""Freeze latest SDK selections and acquire the exact official release wheel.

Selection reads metadata only. Acquisition accepts frozen source identities and
never resolves a latest alias, installs an SDK, or changes dependency locks.
"""
import argparse
import copy
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from . import actions as A
REPOSITORY = A.REPOSITORY
SELECTOR = {'kind': 'release_latest', 'repository': REPOSITORY, 'mode': 'latest-stable'}
_VERSION = re.compile(r'v?(?P<version>[0-9]+\.[0-9]+\.[0-9]+)\Z')
_WHEEL = re.compile(r'hrx_loom-(?P<version>[0-9]+\.[0-9]+\.[0-9]+)-py3-none-manylinux_[0-9]+_[0-9]+_x86_64\.whl\Z')


class ReleaseError(A.ActionsError):
    pass


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', value):
        raise ReleaseError('Release asset lacks a trusted upstream SHA256 digest', 'integrity')
    return value


def _url(tag, name):
    return f'https://github.com/{REPOSITORY}/releases/download/{urllib.parse.quote(tag, safe="")}/{urllib.parse.quote(name, safe="")}'


class GitHubClient(A.GitHubClient):
    @staticmethod
    def _error(error):
        original = A.GitHubClient._error(error)
        return ReleaseError(str(original).replace('Upstream Actions', 'Upstream release'), original.kind)

    def download_release(self, asset_id, destination, expected_size):
        request = self._request(f'/repos/{REPOSITORY}/releases/assets/{A.positive(asset_id, "asset_id")}')
        request.add_header('Accept', 'application/octet-stream')
        try:
            try:
                response = urllib.request.build_opener(A._NoRedirect()).open(request, timeout=60)
            except urllib.error.HTTPError as error:
                if error.code != 302:
                    raise
                target = urllib.parse.urlsplit(error.headers.get('Location', ''))
                error.close()
                allowed = target.hostname and (target.hostname.endswith('.githubusercontent.com')
                    or target.hostname.endswith('.blob.core.windows.net'))
                if target.scheme != 'https' or not allowed or target.username or target.password:
                    raise ReleaseError('Release asset redirect is not approved HTTPS storage', 'provenance')
                # Authorization is restricted to the GitHub API request above.
                response = urllib.request.urlopen(urllib.request.Request(target.geturl(),
                    headers={'User-Agent': 'hrx-systems-test-release/1'}), timeout=60)
            count = 0
            with response, Path(destination).open('xb') as output:
                while chunk := response.read(1024 * 1024):
                    count += len(chunk)
                    if count > expected_size or count > A.MAX_WHEEL_BYTES:
                        raise ReleaseError('Release wheel exceeds frozen asset size', 'integrity')
                    output.write(chunk)
            if count != expected_size:
                raise ReleaseError('Release wheel transfer differs from frozen asset size', 'integrity')
        except (urllib.error.HTTPError, urllib.error.URLError) as error:
            raise self._error(error) from None


def _tag_commit(client, tag):
    reference = client.json(f'/repos/{REPOSITORY}/git/ref/tags/{urllib.parse.quote(tag, safe="")}')
    if reference.get('ref') != 'refs/tags/' + tag:
        raise ReleaseError('Release tag reference disagrees with selected release')
    obj = reference.get('object') or {}
    tag_sha = A.sha(obj.get('sha'))
    visited = set()
    for _ in range(8):
        digest = A.sha(obj.get('sha'))
        if digest in visited:
            raise ReleaseError('Release annotated tag contains a cycle')
        visited.add(digest)
        if obj.get('type') == 'commit':
            return digest, tag_sha
        if obj.get('type') != 'tag':
            raise ReleaseError('Release tag does not resolve to a commit')
        annotated = client.json(f'/repos/{REPOSITORY}/git/tags/{digest}')
        if annotated.get('sha') != digest:
            raise ReleaseError('Annotated tag identity changed during resolution')
        obj = annotated.get('object') or {}
    raise ReleaseError('Release annotated tag chain exceeds resolution limit')


def resolve_source(selector=None, *, client=None, now=None, observe_request=None):
    selector = SELECTOR if selector is None else selector
    if selector != SELECTOR:
        raise ReleaseError('Unsupported latest-release selector', 'configuration')
    client = client or GitHubClient()
    requested = A.stamp(now) if isinstance(now, str) else now or datetime.now(timezone.utc)
    if not isinstance(requested, datetime) or requested.tzinfo is None:
        raise ReleaseError('Requested observation time must include a UTC offset', 'configuration')
    requested = requested.astimezone(timezone.utc)
    if observe_request is not None:
        observe_request({'kind': 'release', 'repository': REPOSITORY,
                         'selection_mode': 'latest-stable', 'requested_at': requested.isoformat()})
    release = client.json(f'/repos/{REPOSITORY}/releases/latest')
    if release.get('draft') is not False or release.get('prerelease') is not False:
        raise ReleaseError('Latest official release must be published and stable')
    tag = release.get('tag_name')
    version = _VERSION.fullmatch(tag) if isinstance(tag, str) else None
    if not version:
        raise ReleaseError('Latest release tag is not a stable HRX SDK version')
    published = A.stamp(release.get('published_at'))
    if published > requested:
        raise ReleaseError('Release publication timestamp is after selection time')
    release_id = A.positive(release.get('id'), 'release_id')
    assets = release.get('assets')
    if not isinstance(assets, list):
        raise ReleaseError('Release metadata omitted assets')
    candidates = []
    for asset in assets:
        if not isinstance(asset, dict):
            raise ReleaseError('Malformed release asset metadata')
        name = asset.get('name')
        match = _WHEEL.fullmatch(name) if isinstance(name, str) else None
        if match:
            candidates.append((asset, match))
    if len(candidates) != 1:
        raise ReleaseError('Latest release has missing or ambiguous canonical Linux Release SDK assets', 'acquisition')
    asset, filename = candidates[0]
    if filename['version'] != version['version']:
        raise ReleaseError('Release asset version disagrees with release tag', 'integrity')
    asset_id, size = A.positive(asset.get('id'), 'asset_id'), A.positive(asset.get('size'), 'asset size')
    if size > A.MAX_WHEEL_BYTES:
        raise ReleaseError('Release wheel exceeds acquisition limit', 'integrity')
    if asset.get('state') != 'uploaded':
        raise ReleaseError('Latest release SDK asset is not fully uploaded', 'acquisition')
    upstream_digest = _digest(asset.get('digest'))
    url = _url(tag, asset['name'])
    if asset.get('browser_download_url') != url:
        raise ReleaseError('Release asset URL disagrees with canonical selected source')
    tested, tag_sha = _tag_commit(client, tag)
    source = {'kind': 'release', 'repository': REPOSITORY, 'selection_mode': 'latest-stable',
        'requested_at': requested.isoformat(), 'release_id': release_id, 'release': tag,
        'published_at': release['published_at'], 'tested_sha': tested, 'tag_sha': tag_sha,
        'asset_id': asset_id, 'version': version['version'],
        'asset': {'id': asset_id, 'name': asset['name'], 'size_bytes': size,
                  'upstream_digest': upstream_digest, 'url': url}}
    return validate_source(source)


def validate_source(source):
    """Validate a frozen release selection without touching the network."""
    if not isinstance(source, dict):
        raise ReleaseError('Frozen release source must be an object', 'configuration')
    if (source.get('kind') != 'release' or source.get('repository') != REPOSITORY
            or source.get('selection_mode') != 'latest-stable'):
        raise ReleaseError('Unapproved frozen release source', 'configuration')
    A.sha(source.get('tested_sha')); A.sha(source.get('tag_sha'))
    A.positive(source.get('release_id'), 'release_id')
    requested, published = A.stamp(source.get('requested_at')), A.stamp(source.get('published_at'))
    if published > requested:
        raise ReleaseError('Frozen release publication is after its selection')
    tag = source.get('release')
    match = _VERSION.fullmatch(tag) if isinstance(tag, str) else None
    if not match or source.get('version') != match['version']:
        raise ReleaseError('Frozen release version/tag changed', 'integrity')
    allowed = {'kind', 'repository', 'selection_mode', 'requested_at', 'release_id', 'release',
               'published_at', 'tested_sha', 'tag_sha', 'asset_id', 'version', 'asset'}
    if source.keys() - allowed:
        raise ReleaseError('Frozen release source contains unknown fields', 'configuration')
    asset = source.get('asset') or {}
    if not isinstance(asset, dict):
        raise ReleaseError('Frozen release asset must be an object', 'configuration')
    if asset.keys() - {'id', 'name', 'size_bytes', 'upstream_digest', 'url', 'sha256', 'digest_verified'}:
        raise ReleaseError('Frozen release asset contains unknown fields', 'configuration')
    name = asset.get('name')
    filename = _WHEEL.fullmatch(name) if isinstance(name, str) else None
    if not filename or filename['version'] != match['version']:
        raise ReleaseError('Frozen release filename/version changed', 'integrity')
    asset_id = A.positive(asset.get('id'), 'asset_id')
    if source.get('asset_id') != asset_id:
        raise ReleaseError('Frozen release asset identity changed', 'integrity')
    size = A.positive(asset.get('size_bytes'), 'asset size')
    if size > A.MAX_WHEEL_BYTES:
        raise ReleaseError('Frozen release asset exceeds size limit', 'integrity')
    upstream = _digest(asset.get('upstream_digest'))
    if ('sha256' in asset and asset['sha256'] != upstream.split(':', 1)[1]) or ('digest_verified' in asset and asset['digest_verified'] is not True):
        raise ReleaseError('Frozen release observed digest disagrees with upstream identity', 'integrity')
    if asset.get('url') != _url(tag, asset['name']):
        raise ReleaseError('Frozen release URL changed', 'integrity')
    return copy.deepcopy(source)


def _actions_source(source):
    if (not isinstance(source, dict) or source.get('kind') != 'github_actions'
            or source.get('repository') != REPOSITORY or source.get('relation_to_head') not in ('equal', 'behind')
            or source.get('workflow_path') not in A.WORKFLOWS):
        raise ReleaseError('Unapproved frozen Actions source', 'configuration')
    allowed = {'kind', 'repository', 'requested_head_sha', 'tested_sha', 'relation_to_head',
        'requested_at', 'build_created_at', 'build_completed_at', 'build_completion_time_source',
        'build_age_seconds', 'release', 'asset_id', 'workflow_path', 'run_id', 'run_attempt',
        'run_url', 'artifact', 'expected_wheel_sha256'}
    if source.keys() - allowed:
        raise ReleaseError('Frozen Actions source contains unknown fields', 'configuration')
    for name in ('tested_sha', 'requested_head_sha'): A.sha(source.get(name))
    if (source['relation_to_head'] == 'equal') != (source['tested_sha'] == source['requested_head_sha']):
        raise ReleaseError('Frozen Actions HEAD relation changed', 'integrity')
    requested = A.stamp(source.get('requested_at'))
    created = A.stamp(source.get('build_created_at'))
    completed = A.stamp(source.get('build_completed_at'))
    age = source.get('build_age_seconds')
    if (not created <= completed <= requested or type(age) not in (int, float)
            or not math.isclose(age, (requested-completed).total_seconds(), abs_tol=0.001)):
        raise ReleaseError('Frozen Actions build timestamps/age changed', 'integrity')
    run_id = A.positive(source.get('run_id'), 'run_id'); A.positive(source.get('run_attempt'), 'run_attempt')
    if source.get('run_url') != f'https://github.com/{REPOSITORY}/actions/runs/{run_id}':
        raise ReleaseError('Frozen Actions run URL changed', 'integrity')
    if source.get('release') is not None or source.get('asset_id') is not None:
        raise ReleaseError('Frozen Actions source mixes release identity', 'integrity')
    artifact = source.get('artifact') or {}
    if not isinstance(artifact, dict) or artifact.get('name') != A.ARTIFACT_NAME:
        raise ReleaseError('Frozen Actions artifact changed', 'integrity')
    if artifact.keys() - {'id', 'name', 'url', 'created_at', 'expires_at', 'size_bytes',
                          'archive_sha256', 'upstream_digest', 'digest_verified'}:
        raise ReleaseError('Frozen Actions artifact contains unknown fields', 'configuration')
    identity = A.positive(artifact.get('id'), 'artifact_id')
    if artifact.get('url') != f'https://github.com/{REPOSITORY}/actions/runs/{run_id}/artifacts/{identity}':
        raise ReleaseError('Frozen Actions artifact URL changed', 'integrity')
    if not created <= A.stamp(artifact.get('created_at')) <= completed or A.stamp(artifact.get('expires_at')) <= requested:
        raise ReleaseError('Frozen Actions artifact timestamps changed', 'integrity')
    size = A.positive(artifact.get('size_bytes'), 'artifact size')
    if size > A.MAX_ARCHIVE_BYTES: raise ReleaseError('Frozen Actions artifact exceeds size limit', 'integrity')
    upstream = _digest(artifact.get('upstream_digest')).split(':', 1)[1]
    verified = artifact.get('digest_verified')
    if ((verified is True and artifact.get('archive_sha256') != upstream)
            or (verified is False and artifact.get('archive_sha256') is not None)
            or type(verified) is not bool):
        raise ReleaseError('Frozen Actions observed artifact digest changed', 'integrity')
    if source.get('expected_wheel_sha256'): A.sha(source['expected_wheel_sha256'], 64)
    return copy.deepcopy(source)


def freeze_selection(selector, *, client=None, now=None, observe_request=None):
    """Resolve metadata once; the returned reference contains no payload bytes."""
    if selector == SELECTOR:
        source = resolve_source(selector, client=client, now=now, observe_request=observe_request)
    elif isinstance(selector, dict) and selector.get('kind') == 'github_actions':
        source = A.resolve_source(selector, client=client, now=now, observe_request=observe_request)
    else:
        raise ReleaseError('Unsupported runtime selection reference', 'configuration')
    return load_selection({'schema_version': 1, 'selector': copy.deepcopy(selector), 'source': source})


def load_selection(value):
    """Load/copy a frozen reference without resolving latest or downloading."""
    if isinstance(value, (str, Path)):
        value = json.loads(Path(value).read_text())
    if not isinstance(value, dict) or value.get('schema_version') != 1:
        raise ReleaseError('Unsupported frozen runtime selection schema', 'configuration')
    selector, source = value.get('selector'), value.get('source')
    if selector == SELECTOR:
        source = validate_source(source)
    elif (isinstance(selector, dict) and selector.get('kind') == 'github_actions'
          and selector.get('repository') == REPOSITORY and selector.get('branch') == 'main'
          and selector.get('mode') in ('latest-successful', 'replay')
          and selector.get('artifact_name') == A.ARTIFACT_NAME):
        allowed = {'kind', 'repository', 'branch', 'mode', 'artifact_name'}
        if selector['mode'] == 'replay':
            allowed |= {'run_id', 'run_attempt', 'artifact_id', 'wheel_sha256'}
        if selector.keys() - allowed:
            raise ReleaseError('Frozen Actions selector contains unknown fields', 'configuration')
        source = _actions_source(source)
        if selector['mode'] != 'replay' and source.get('expected_wheel_sha256'):
            raise ReleaseError('Latest Actions source contains replay-only digest', 'integrity')
        if selector['mode'] == 'replay':
            for field, source_field in (('run_id', 'run_id'), ('run_attempt', 'run_attempt'), ('artifact_id', None)):
                observed = (source['artifact']['id'] if source_field is None else source[source_field])
                if A.positive(selector.get(field), field) != observed:
                    raise ReleaseError('Frozen replay selection identity changed', 'integrity')
            if A.sha(selector.get('wheel_sha256'), 64) != source.get('expected_wheel_sha256'):
                raise ReleaseError('Frozen replay wheel digest changed', 'integrity')
    else:
        raise ReleaseError('Unapproved frozen runtime selector', 'configuration')
    return {'schema_version': 1, 'selector': copy.deepcopy(selector), 'source': source}


def acquire_wheel(source, destination, *, dependency_lock, environment, client=None, qualifier=None):
    source = validate_source(source)
    asset = source['asset']
    destination = Path(destination)
    if destination.exists() or destination.is_symlink() or any(p.is_symlink() for p in destination.parents):
        raise ReleaseError('Release wheel destination must be a new per-attempt directory without symlink parents', 'configuration')
    destination.mkdir(parents=True)
    target = destination / asset['name']
    try:
        with tempfile.TemporaryDirectory(prefix='.release-', dir=destination) as temporary:
            downloaded = Path(temporary) / asset['name']
            (client or GitHubClient()).download_release(asset['id'], downloaded, asset['size_bytes'])
            if downloaded.stat().st_size != asset['size_bytes'] or A._digest(downloaded) != asset['upstream_digest'].split(':', 1)[1]:
                raise ReleaseError('Release wheel differs from frozen size or SHA256', 'integrity')
            if qualifier:
                metadata, status = qualifier(downloaded, dependency_lock, environment)
            else:
                metadata = A.wheel_metadata(downloaded, environment=environment)
                status = A.validate_dependencies(metadata, dependency_lock, environment=environment)
            package_name = metadata.get('name', '')
            if not isinstance(package_name, str) or re.sub(r'[-_.]+', '-', package_name).lower() != 'hrx-loom' or metadata.get('version') != source['version']:
                raise ReleaseError('Release wheel metadata disagrees with selected release', 'integrity')
            downloaded.replace(target)
        source['asset'].update(sha256=asset['upstream_digest'].split(':', 1)[1], digest_verified=True)
        metadata = {**metadata, 'metadata_source': 'wheel METADATA/WHEEL; source commit associated via release tag'}
        wheel = {'filename': target.name, 'name': metadata['name'], 'version': metadata['version'],
            'size_bytes': target.stat().st_size, 'sha256': source['asset']['sha256'],
            'source_url': asset['url'], 'url': asset['url'],
            'source_commit_evidence': 'GitHub release tag resolved to commit; not independently embedded wheel commit'}
        return {'source': source, 'wheel': wheel, 'metadata': metadata,
                'compatibility_status': status, 'staged_path': str(target)}
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='operation', required=True)
    freeze = commands.add_parser('freeze')
    A.add_source_arguments(freeze)
    freeze.set_defaults(runtime_source='release-latest')
    freeze.add_argument('--output', type=Path, required=True)
    verify = commands.add_parser('verify')
    verify.add_argument('--input', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.operation == 'verify':
            result = load_selection(args.input)
        else:
            if args.output.exists() or args.output.is_symlink() or any(p.is_symlink() for p in args.output.parents):
                raise ReleaseError('Frozen selection output must be new', 'configuration')
            result = freeze_selection(A.selector_from_args(args))
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open('x') as stream:
                stream.write(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result, indent=2))
    except (A.ActionsError, OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f'runtime selection failed: {error}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
