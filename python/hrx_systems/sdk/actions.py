#!/usr/bin/env python3
"""Select one trusted upstream Actions SDK and verify its per-attempt wheel.

No SDK installation, model acquisition, persistent cache or serving occurs here.
"""
import argparse
import copy
from datetime import datetime, timezone
from email.parser import BytesParser
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile

REPOSITORY = 'ROCm/hrx-system'
ARTIFACT_NAME = 'loom-sdk-wheel-linux-Release'
WORKFLOWS = ('.github/workflows/ci_iree.yml', '.github/workflows/build_loom_sdk.yml')
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_WHEEL_BYTES = 512 * 1024 * 1024
MAX_MEMBERS = 128
MAX_METADATA_BYTES = 2 * 1024 * 1024
MAX_PAGES = 20


class ActionsError(ValueError):
    def __init__(self, message, kind='provenance'):
        super().__init__(message)
        self.kind = kind


def stamp(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None:
            raise ValueError()
        return result.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError):
        raise ActionsError('Invalid upstream UTC timestamp') from None


def positive(value, name):
    if type(value) is int and value > 0:
        return value
    if isinstance(value, str) and re.fullmatch(r'[1-9][0-9]*', value):
        return int(value)
    raise ActionsError(f'{name} requires a positive decimal integer', 'configuration')


def sha(value, length=40):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{' + str(length) + '}', value):
        raise ActionsError('Invalid source or payload digest', 'configuration')
    return value


def add_source_arguments(parser):
    parser.add_argument('--runtime-source', choices=('release', 'release-latest', 'actions-main', 'actions-replay'))
    parser.add_argument('--runtime-run-id')
    parser.add_argument('--runtime-run-attempt')
    parser.add_argument('--runtime-artifact-id')
    parser.add_argument('--runtime-wheel-sha256')


def selector_from_args(args, configured=None):
    mode = getattr(args, 'runtime_source', None)
    values = {name: getattr(args, 'runtime_' + name, None) for name in
              ('run_id', 'run_attempt', 'artifact_id', 'wheel_sha256')}
    if mode != 'actions-replay' and any(value is not None for value in values.values()):
        raise ActionsError('Replay identifiers require --runtime-source actions-replay', 'configuration')
    if mode is None:
        return copy.deepcopy(configured)
    if mode == 'release-latest':
        return {'kind': 'release_latest', 'repository': REPOSITORY, 'mode': 'latest-stable'}
    if mode == 'release':
        if configured and configured.get('kind') == 'release':
            return copy.deepcopy(configured)
        raise ActionsError('Release mode requires an explicitly configured pinned release', 'configuration')
    selector = {'kind': 'github_actions', 'repository': REPOSITORY, 'branch': 'main',
                'mode': 'replay' if mode == 'actions-replay' else 'latest-successful',
                'artifact_name': ARTIFACT_NAME}
    if mode == 'actions-replay':
        selector.update({name: positive(values[name], name) for name in ('run_id', 'run_attempt', 'artifact_id')})
        selector['wheel_sha256'] = sha(values['wheel_sha256'], 64)
    return selector


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class GitHubClient:
    """Credential is used only for api.github.com, never signed storage URLs."""
    def __init__(self, token=None):
        self.token = token if token is not None else os.environ.get('HRX_ACTIONS_TOKEN')

    def _request(self, path):
        if not path.startswith('/repos/' + REPOSITORY + '/') or '..' in path.split('/'):
            raise ActionsError('Unapproved upstream API path')
        headers = {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28',
                   'User-Agent': 'hrx-systems-test-actions/1'}
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        return urllib.request.Request('https://api.github.com' + path, headers=headers)

    @staticmethod
    def _error(error):
        if isinstance(error, urllib.error.HTTPError):
            if error.code in (401, 403):
                reason = 'rate limit' if error.headers.get('X-RateLimit-Remaining') == '0' else 'permission/authentication'
                return ActionsError(f'Upstream Actions API {reason} failure (HTTP {error.code}); check HRX_ACTIONS_TOKEN with upstream Actions read access', 'authentication')
            return ActionsError(f'Upstream Actions API failed (HTTP {error.code})', 'acquisition')
        return ActionsError('Upstream Actions API/transfer could not be reached', 'acquisition')

    def json(self, path):
        try:
            with urllib.request.urlopen(self._request(path), timeout=60) as response:
                data = response.read(16 * 1024 * 1024 + 1)
            if len(data) > 16 * 1024 * 1024:
                raise ActionsError('Upstream API metadata exceeds limit')
            value = json.loads(data)
            if not isinstance(value, dict):
                raise ActionsError('Upstream API returned invalid metadata')
            return value
        except (urllib.error.HTTPError, urllib.error.URLError) as error:
            raise self._error(error) from None
        except (UnicodeError, json.JSONDecodeError):
            raise ActionsError('Upstream API returned malformed JSON') from None

    def download(self, artifact_id, destination, expected_size):
        if not self.token:
            raise ActionsError('Set HRX_ACTIONS_TOKEN with Actions read permission for ROCm/hrx-system', 'authentication')
        path = f'/repos/{REPOSITORY}/actions/artifacts/{positive(artifact_id, "artifact_id")}/zip'
        try:
            try:
                response = urllib.request.build_opener(_NoRedirect()).open(self._request(path), timeout=60)
            except urllib.error.HTTPError as error:
                if error.code != 302:
                    raise
                target = urllib.parse.urlsplit(error.headers.get('Location', ''))
                error.close()
                allowed = target.hostname and (target.hostname.endswith('.blob.core.windows.net')
                    or target.hostname.endswith('.githubusercontent.com')
                    or target.hostname.endswith('.actions.githubusercontent.com'))
                if target.scheme != 'https' or not allowed or target.username or target.password:
                    raise ActionsError('Upstream artifact redirect is not an approved HTTPS storage URL')
                # Never forward the API Authorization header across this boundary.
                request = urllib.request.Request(target.geturl(), headers={'User-Agent': 'hrx-systems-test-actions/1'})
                response = urllib.request.urlopen(request, timeout=60)
            with response, Path(destination).open('wb') as output:
                count = 0
                while chunk := response.read(1024 * 1024):
                    count += len(chunk)
                    if count > expected_size or count > MAX_ARCHIVE_BYTES:
                        raise ActionsError('Artifact exceeds recorded archive size', 'integrity')
                    output.write(chunk)
            if count != expected_size:
                raise ActionsError('Artifact transfer size differs from recorded archive', 'integrity')
        except (urllib.error.HTTPError, urllib.error.URLError) as error:
            raise self._error(error) from None


def _pages(client, path, key):
    join = '&' if '?' in path else '?'
    for page in range(1, MAX_PAGES + 1):
        value = client.json(path + f'{join}per_page=100&page={page}')
        items = value.get(key)
        if not isinstance(items, list):
            raise ActionsError(f'Upstream API omitted {key}')
        yield from items
        if len(items) < 100:
            return
    raise ActionsError('Upstream candidate pagination exceeded the bounded search; use explicit replay')


def _eligible(run, requested):
    if not isinstance(run, dict):
        raise ActionsError('Invalid upstream run record')
    return (run.get('path') in WORKFLOWS and run.get('event') in ('push', 'workflow_dispatch')
        and run.get('head_branch') == 'main' and run.get('status') == 'completed'
        and run.get('conclusion') == 'success'
        and (run.get('repository') or {}).get('full_name') == REPOSITORY
        and (run.get('head_repository') or {}).get('full_name') == REPOSITORY
        and stamp(run.get('created_at')) <= requested and stamp(run.get('updated_at')) <= requested)


def _artifact(client, run, requested, wanted_id=None):
    matches = []
    for item in _pages(client, f'/repos/{REPOSITORY}/actions/runs/{run["id"]}/artifacts', 'artifacts'):
        if not isinstance(item, dict):
            raise ActionsError('Invalid upstream artifact record')
        if item.get('name') != ARTIFACT_NAME or (wanted_id is not None and item.get('id') != wanted_id):
            continue
        if item.get('expired') is not False or stamp(item.get('expires_at')) <= requested:
            continue
        owner = item.get('workflow_run') or {}
        if owner.get('id') != run['id'] or owner.get('head_sha') != run.get('head_sha') or owner.get('head_branch') != 'main':
            raise ActionsError('Artifact does not belong to the selected main run')
        created = stamp(item.get('created_at'))
        started = stamp(run.get('run_started_at') or run.get('created_at'))
        if not started <= created <= stamp(run['updated_at']):
            raise ActionsError('Artifact cannot be associated with selected run attempt')
        digest = item.get('digest')
        if not isinstance(digest, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
            raise ActionsError('Artifact has no supported SHA256 provenance digest')
        size = item.get('size_in_bytes')
        if type(size) is not int or not 0 < size <= MAX_ARCHIVE_BYTES:
            raise ActionsError('Artifact archive size is invalid or exceeds limit')
        matches.append(item)
    if len(matches) > 1:
        raise ActionsError('Selected run has ambiguous same-name artifacts')
    return matches[0] if matches else None


def resolve_source(selector, *, client=None, now=None, observe_request=None):
    """Freeze one trusted source. This does not download or verify wheel bytes."""
    if not isinstance(selector, dict) or selector.get('kind') != 'github_actions':
        raise ActionsError('Expected github_actions source configuration', 'configuration')
    if selector.get('repository') != REPOSITORY or selector.get('branch') != 'main' or selector.get('artifact_name') != ARTIFACT_NAME:
        raise ActionsError('Actions source must be the allowlisted upstream main Linux Release artifact', 'configuration')
    if selector.get('mode') not in ('latest-successful', 'replay'):
        raise ActionsError('Unknown Actions selection mode', 'configuration')
    client = client or GitHubClient()
    head = sha(client.json(f'/repos/{REPOSITORY}/commits/main').get('sha'))
    requested = stamp(now) if isinstance(now, str) else (now or datetime.now(timezone.utc))
    if not isinstance(requested, datetime) or requested.tzinfo is None:
        raise ActionsError('Requested observation time must include a UTC offset', 'configuration')
    requested = requested.astimezone(timezone.utc)
    if observe_request is not None:
        observe_request({'kind': 'github_actions', 'repository': REPOSITORY,
                         'requested_head_sha': head, 'requested_at': requested.isoformat()})
    replay = selector['mode'] == 'replay'
    if replay:
        run_id = positive(selector.get('run_id'), 'run_id')
        expected_attempt = positive(selector.get('run_attempt'), 'run_attempt')
        wanted_artifact = positive(selector.get('artifact_id'), 'artifact_id')
        sha(selector.get('wheel_sha256'), 64)
        run = client.json(f'/repos/{REPOSITORY}/actions/runs/{run_id}')
        if run.get('run_attempt') != expected_attempt:
            raise ActionsError('Replay run attempt no longer matches; refusing replacement')
        candidates = [run]
    else:
        candidates_by_id = {}
        for path in WORKFLOWS:
            endpoint = f'/repos/{REPOSITORY}/actions/workflows/{path.rsplit("/", 1)[1]}/runs?branch=main&status=success'
            for run in _pages(client, endpoint, 'workflow_runs'):
                if _eligible(run, requested):
                    candidates_by_id[positive(run.get('id'), 'run_id')] = run
        candidates = sorted(candidates_by_id.values(), key=lambda run: (stamp(run['created_at']), run['id']), reverse=True)
        candidates.sort(key=lambda run: run.get('head_sha') != head)
        wanted_artifact = None
    for run in candidates:
        if not _eligible(run, requested):
            if replay:
                raise ActionsError('Replay run is not a successful eligible upstream main build')
            continue
        tested = sha(run.get('head_sha'))
        artifact = _artifact(client, run, requested, wanted_artifact)
        if artifact is None:
            if replay:
                raise ActionsError('Replay artifact is unavailable or expired; no replacement selected', 'acquisition')
            continue
        relation = 'equal'
        if tested != head:
            compared = client.json(f'/repos/{REPOSITORY}/compare/{tested}...{head}')
            if compared.get('status') != 'ahead' or (compared.get('merge_base_commit') or {}).get('sha') != tested:
                raise ActionsError('Selected SDK source is not a verified ancestor of requested main HEAD')
            relation = 'behind'
        source = {'kind': 'github_actions', 'repository': REPOSITORY, 'requested_head_sha': head,
            'tested_sha': tested, 'relation_to_head': relation, 'requested_at': requested.isoformat(),
            'build_created_at': run['created_at'], 'build_completed_at': run['updated_at'],
            'build_completion_time_source': 'run.updated_at (conservative completion bound)',
            'build_age_seconds': (requested - stamp(run['updated_at'])).total_seconds(),
            'release': None, 'asset_id': None, 'workflow_path': run['path'], 'run_id': run['id'],
            'run_attempt': positive(run.get('run_attempt'), 'run_attempt'),
            'run_url': f'https://github.com/{REPOSITORY}/actions/runs/{run["id"]}',
            'artifact': {'id': artifact['id'], 'name': ARTIFACT_NAME,
                'url': f'https://github.com/{REPOSITORY}/actions/runs/{run["id"]}/artifacts/{artifact["id"]}',
                'created_at': artifact['created_at'], 'expires_at': artifact['expires_at'],
                'size_bytes': artifact['size_in_bytes'], 'archive_sha256': None,
                'upstream_digest': artifact['digest'], 'digest_verified': False}}
        if replay:
            source['expected_wheel_sha256'] = selector['wheel_sha256']
        return source
    raise ActionsError('No successful eligible upstream main SDK artifact is available', 'acquisition')


def _packaging():
    try:
        from pip._vendor.packaging.requirements import Requirement
        from pip._vendor.packaging.specifiers import SpecifierSet
        from pip._vendor.packaging.utils import canonicalize_name, parse_wheel_filename
        return Requirement, SpecifierSet, canonicalize_name, parse_wheel_filename
    except ImportError:
        raise ActionsError('Bootstrap pip packaging support is required for offline wheel qualification', 'dependency_incompatible') from None


def wheel_metadata(path, *, environment):
    """Inspect wheel metadata/tags for the selected target, never the host by guess."""
    _, SpecifierSet, canonical, parse_filename = _packaging()
    try:
        name, version, _, filename_tags = parse_filename(Path(path).name)
    except ValueError:
        raise ActionsError('Invalid SDK wheel filename', 'integrity') from None
    if canonical(name) != 'hrx-loom':
        raise ActionsError('Artifact wheel is not hrx-loom', 'integrity')
    python_version = environment.get('python_full_version') or environment.get('python_version')
    if not python_version or environment.get('sys_platform') != 'linux' or environment.get('platform_machine') != 'x86_64':
        raise ActionsError('Actions SDK requires an explicit Linux x86_64 target Python environment', 'dependency_incompatible')
    try:
        glibc = tuple(int(part) for part in environment['glibc_version'].split('.'))
    except (KeyError, ValueError, AttributeError):
        raise ActionsError('Target glibc version must be explicitly known', 'dependency_incompatible') from None
    compatible = False
    for tag in filename_tags:
        match = re.fullmatch(r'manylinux_(\d+)_(\d+)_x86_64', tag.platform)
        if match and tuple(map(int, match.groups())) <= glibc and tag.interpreter in ('py3', 'cp' + python_version.replace('.', '')[:3]) and tag.abi in ('none', 'abi3', 'cp' + python_version.replace('.', '')[:3]):
            compatible = True
    if not compatible:
        raise ActionsError('SDK wheel tags are incompatible with target Python/architecture/glibc', 'dependency_incompatible')
    try:
        with zipfile.ZipFile(path) as wheel:
            members = wheel.infolist()
            if len(members) > 20000 or sum(item.file_size for item in members) > 2 * 1024**3:
                raise ActionsError('Wheel contents exceed qualification limits', 'integrity')
            for item in members:
                _member(item)
            candidates = [item for item in members if item.filename.endswith('.dist-info/METADATA')]
            wheels = [item for item in members if item.filename.endswith('.dist-info/WHEEL')]
            if len(candidates) != 1 or len(wheels) != 1 or candidates[0].file_size > MAX_METADATA_BYTES or wheels[0].file_size > MAX_METADATA_BYTES:
                raise ActionsError('Wheel metadata is ambiguous or oversized', 'integrity')
            metadata = BytesParser().parsebytes(wheel.read(candidates[0]))
            if candidates[0].filename.rsplit('/', 1)[0] != wheels[0].filename.rsplit('/', 1)[0]:
                raise ActionsError('Wheel METADATA/WHEEL belong to different distributions', 'integrity')
            wheel_info = BytesParser().parsebytes(wheel.read(wheels[0]))
    except zipfile.BadZipFile:
        raise ActionsError('SDK wheel ZIP is malformed', 'integrity') from None
    if canonical(metadata.get('Name', '')) != name or str(metadata.get('Version')) != str(version):
        raise ActionsError('Wheel name/version metadata disagrees with filename', 'integrity')
    declared_tags = set(wheel_info.get_all('Tag', []))
    if declared_tags != {str(tag) for tag in filename_tags}:
        raise ActionsError('Wheel tags disagree with WHEEL metadata', 'integrity')
    requires_python = metadata.get('Requires-Python')
    if requires_python and not SpecifierSet(requires_python).contains(python_version, prereleases=True):
        raise ActionsError(f'SDK Requires-Python {requires_python} conflicts with target {python_version}', 'dependency_incompatible')
    return {'name': str(metadata['Name']), 'version': str(metadata['Version']),
            'requires_python': requires_python, 'requires_dist': metadata.get_all('Requires-Dist', []),
            'metadata_source': 'wheel METADATA/WHEEL; source commit associated via GitHub run'}


def validate_dependencies(metadata, dependency_lock, *, environment, dependency_directory=None):
    """Check active PEP508 requirements against exact locked versions without pip solve."""
    Requirement, _, canonical, _ = _packaging()
    locked = {}
    for package in dependency_lock.get('wheels', []):
        key = canonical(package['name'])
        if key in locked:
            raise ActionsError('Dependency lock has duplicate package names', 'dependency_incompatible')
        locked[key] = package
    if dependency_lock.get('python') and dependency_lock['python'] != environment.get('python_version'):
        raise ActionsError('Dependency lock Python differs from selected target', 'dependency_incompatible')
    if dependency_lock.get('platform') and dependency_lock['platform'] != 'linux_x86_64':
        raise ActionsError('Dependency lock platform differs from selected target', 'dependency_incompatible')
    # The harness exercises loom-fetch/Hugging Face even though the SDK marks it optional.
    pending = [(metadata['name'], metadata.get('requires_dist', []), extra) for extra in ('', 'hf')]
    checked = set()
    while pending:
        owner, requirements, extra = pending.pop()
        if (owner, extra) in checked:
            continue
        checked.add((owner, extra))
        for text in requirements:
            try:
                requirement = Requirement(text)
                marker_environment = dict.fromkeys(('implementation_name', 'implementation_version', 'os_name',
                    'platform_machine', 'platform_python_implementation', 'platform_release', 'platform_system',
                    'platform_version', 'python_full_version', 'python_version', 'sys_platform'), '')
                marker_environment.update(environment, extra=extra)
                active = requirement.marker is None or requirement.marker.evaluate(marker_environment)
            except Exception:
                raise ActionsError(f'Invalid requirement metadata in {owner}', 'dependency_incompatible') from None
            if not active:
                continue
            package = locked.get(canonical(requirement.name))
            if requirement.url or package is None or not requirement.specifier.contains(package['version'], prereleases=True):
                actual = 'not locked' if package is None else package['version']
                raise ActionsError(f'dependency_incompatible: {owner} requires {requirement}; locked {actual}', 'dependency_incompatible')
            if dependency_directory is not None:
                path = Path(dependency_directory) / package['filename']
                if not path.is_file() or path.is_symlink() or _digest(path) != package['sha256']:
                    raise ActionsError(f'Locked dependency bytes are unavailable or changed: {package["name"]}', 'integrity')
                try:
                    with zipfile.ZipFile(path) as wheel:
                        names = [name for name in wheel.namelist() if name.endswith('.dist-info/METADATA')]
                        if len(names) != 1 or wheel.getinfo(names[0]).file_size > MAX_METADATA_BYTES:
                            raise ActionsError('Dependency wheel metadata is ambiguous or oversized', 'integrity')
                        child = BytesParser().parsebytes(wheel.read(names[0]))
                except zipfile.BadZipFile:
                    raise ActionsError('Dependency wheel ZIP is malformed', 'integrity') from None
                if canonical(child.get('Name', '')) != canonical(package['name']) or child.get('Version') != package['version']:
                    raise ActionsError('Dependency wheel metadata disagrees with lock', 'integrity')
                for requested_extra in ('', *sorted(requirement.extras)):
                    pending.append((package['name'], child.get_all('Requires-Dist', []), requested_extra))
    return 'locked_metadata_compatible' if dependency_directory is not None else 'direct_requirements_compatible_pending_offline_install'


def _member(item):
    relative = PurePosixPath(item.filename)
    if not item.filename or relative.is_absolute() or '..' in relative.parts or '\\' in item.filename:
        raise ActionsError('Unsafe archive member path', 'integrity')
    mode = item.external_attr >> 16
    if stat.S_ISLNK(mode) or (mode and stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
        raise ActionsError('Archive contains a symlink or special file', 'integrity')
    return relative


def _digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def acquire_wheel(source, destination, *, dependency_lock, environment, client=None, qualifier=None):
    """Acquire exactly the frozen artifact once; return bytes-backed wheel/source facts."""
    source = copy.deepcopy(source)
    artifact = source.get('artifact') or {}
    if source.get('kind') != 'github_actions' or source.get('repository') != REPOSITORY or artifact.get('name') != ARTIFACT_NAME:
        raise ActionsError('Unapproved frozen Actions source', 'configuration')
    sha(source.get('tested_sha'))
    digest = artifact.get('upstream_digest', '')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
        raise ActionsError('Frozen artifact lacks expected digest', 'integrity')
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise ActionsError('Actions wheel destination must be a new per-attempt directory', 'configuration')
    if any(parent.is_symlink() for parent in destination.parents):
        raise ActionsError('Actions wheel destination parents must not be symlinks', 'configuration')
    destination.mkdir(parents=True)
    target = None
    try:
        with tempfile.TemporaryDirectory(prefix='.artifact-', dir=destination) as temporary:
            archive = Path(temporary) / 'artifact.zip'
            (client or GitHubClient()).download(artifact['id'], archive, artifact['size_bytes'])
            observed = _digest(archive)
            if observed != digest.split(':', 1)[1]:
                raise ActionsError('Artifact SHA256 differs from upstream digest', 'integrity')
            try:
                with zipfile.ZipFile(archive) as bundle:
                    members = bundle.infolist()
                    if len(members) > MAX_MEMBERS or sum(item.file_size for item in members) > MAX_WHEEL_BYTES:
                        raise ActionsError('Artifact members exceed extraction limits', 'integrity')
                    paths = [_member(item) for item in members]
                    if len(set(map(str, paths))) != len(paths):
                        raise ActionsError('Artifact contains duplicate member paths', 'integrity')
                    files = [item for item in members if not item.is_dir()]
                    if len(files) != 1 or not files[0].filename.endswith('.whl'):
                        raise ActionsError('Artifact must contain exactly one SDK wheel and no unexpected payloads', 'integrity')
                    item = files[0]
                    target = destination / PurePosixPath(item.filename).name
                    with bundle.open(item) as input_stream, target.open('xb') as output:
                        while chunk := input_stream.read(1024 * 1024):
                            output.write(chunk)
            except zipfile.BadZipFile:
                raise ActionsError('Artifact ZIP is malformed', 'integrity') from None
            if qualifier is None:
                metadata = wheel_metadata(target, environment=environment)
                status = validate_dependencies(metadata, dependency_lock, environment=environment)
            else:
                metadata, status = qualifier(target, dependency_lock, environment)
            checksum = _digest(target)
            if source.get('expected_wheel_sha256') and checksum != source['expected_wheel_sha256']:
                raise ActionsError('Replay wheel SHA256 differs from prior provenance', 'integrity')
            source['artifact'].update(archive_sha256=observed, digest_verified=True)
            wheel = {'filename': target.name, 'name': metadata['name'], 'version': metadata['version'],
                'size_bytes': target.stat().st_size, 'sha256': checksum,
                'source_url': artifact['url'], 'url': artifact['url'],
                'source_commit_evidence': 'GitHub successful main run head_sha; not independently embedded wheel commit'}
            return {'source': source, 'wheel': wheel, 'metadata': metadata,
                    'compatibility_status': status, 'staged_path': str(target)}
    except BaseException:
        if target is not None:
            target.unlink(missing_ok=True)
        raise
