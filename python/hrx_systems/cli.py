"""Fixed, lazy dispatch for repository-owned command roles."""
import argparse
import importlib

# Module names are code, never input supplied by the operator.
ROLES = {
    'prepare-cached-models': 'hrx_systems.preparation.command',
    'run-prepared-models': 'hrx_systems.execution.command',
    'seed-models': 'hrx_systems.cache.seed_assets',
    'seed-cached-models': 'hrx_systems.cache.seed_suite',
    'seed-pipelines': 'hrx_systems.cache.seed_pipelines',
    'download-models': 'hrx_systems.cache.download_catalog',
    'verify-cached-assets': 'hrx_systems.cache.assets',
    'generate-model-report': 'hrx_systems.reporting.render',
    'package-model-failures': 'hrx_systems.reporting.package_failures',
    'qualify-cached-models': 'hrx_systems.host.hardware_advisory',
    'sdk-environment': 'hrx_systems.sdk.environment',
    'release-runtime': 'hrx_systems.sdk.releases',
    'paired-ci': 'hrx_systems.ci.paired',
    'ci-progress': 'hrx_systems.diagnostics.progress',
}


def module_for(role):
    if not isinstance(role, str) or role not in ROLES:
        raise ValueError('unknown repository command role')
    return ROLES[role]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('role', choices=tuple(ROLES))
    parser.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    module = importlib.import_module(module_for(args.role))
    return module.main(args.arguments)
