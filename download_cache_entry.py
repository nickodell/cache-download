"""Attempt to download a GitHub Actions cache entry using the unofficial
artifactcache service API.

Background
----------
GitHub Actions runners access cache via an internal service whose URL is
injected as ACTIONS_CACHE_URL (v1) or ACTIONS_RESULTS_URL (v2) at job start,
and authenticated with a short-lived per-job JWT called ACTIONS_RUNTIME_TOKEN.

Neither the cache service URL nor the runtime token is derivable from a
repository PAT — they are only available inside a running workflow job.
This command therefore cannot succeed outside of a job, but it demonstrates
exactly what the API looks like so the approach could be adapted if those
values were ever available.

V1 API (used here)
------------------
  GET {ACTIONS_CACHE_URL}_apis/artifactcache/cache
      ?keys=<comma-separated-keys>
      &version=<sha256>

  Authorization: Bearer <ACTIONS_RUNTIME_TOKEN>

  200 → {"cacheKey": "...", "archiveLocation": "<signed-download-url>"}
  204 → cache miss

The `version` is sha256(paths.join('|') + '|' + compressionMethod + '|1.0').
Paths are the directories the workflow passed to actions/cache; they are not
stored anywhere accessible outside the job.
"""
import hashlib
import sys

import requests
from django.core.management.base import BaseCommand, CommandError

from actions.models import CacheEntry
from user.models import Repository


def compute_version(paths, compression_method='zstd'):
    """Replicate @actions/cache getCacheVersion."""
    components = list(paths) + [compression_method, '1.0']
    return hashlib.sha256('|'.join(components).encode()).hexdigest()


class Command(BaseCommand):
    help = 'Attempt to download a cache entry via the unofficial artifactcache API'

    def add_arguments(self, parser):
        parser.add_argument('key', help='Cache entry key')
        parser.add_argument('--owner', default='scipy', help='GitHub org (default: scipy)')
        parser.add_argument('--repo', default='scipy', help='GitHub repo (default: scipy)')
        parser.add_argument(
            '--cache-url',
            help='ACTIONS_CACHE_URL (e.g. https://artifactcache.actions.githubusercontent.com/xxx/). '
                 'Only available inside a running workflow job.',
        )
        parser.add_argument(
            '--runtime-token',
            help='ACTIONS_RUNTIME_TOKEN. Only available inside a running workflow job.',
        )
        parser.add_argument(
            '--paths',
            nargs='+',
            metavar='PATH',
            help='Cached paths used to compute the version hash (e.g. ~/.cache/codeql). '
                 'Must match exactly what the workflow passed to actions/cache.',
        )

    def handle(self, *args, **options):
        key = options['key']
        owner = options['owner']
        repo_name = options['repo']
        cache_url = options['cache_url']
        runtime_token = options['runtime_token']
        paths = options['paths']

        # Show what we know from our DB about this entry.
        try:
            repository = Repository.objects.select_related('organization').get(
                organization__name=owner, name=repo_name,
            )
        except Repository.DoesNotExist:
            raise CommandError(f'Repository {owner}/{repo_name} not found in DB')

        entry = CacheEntry.objects.filter(repository=repository, key=key).first()
        if entry:
            self.stdout.write(f'Found CacheEntry in DB:')
            self.stdout.write(f'  github_id : {entry.github_id}')
            self.stdout.write(f'  key       : {entry.key}')
            self.stdout.write(f'  size      : {entry.size_bytes:,} bytes')
            self.stdout.write(f'  ref       : {entry.ref}')
            self.stdout.write(f'  pruned    : {entry.pruned}')
        else:
            self.stdout.write(self.style.WARNING(f'Key {key!r} not found in local DB.'))

        self.stdout.write('')

        # Explain what tokens are needed.
        if not cache_url or not runtime_token:
            self.stdout.write(self.style.WARNING(
                'Cannot contact the cache service: --cache-url and --runtime-token are required.\n'
                '\n'
                'These values are only available inside a running GitHub Actions job:\n'
                '  ACTIONS_CACHE_URL    — injected by the runner, looks like\n'
                '                         https://artifactcache.actions.githubusercontent.com/<scope>/\n'
                '  ACTIONS_RUNTIME_TOKEN — a short-lived per-job JWT; a PAT cannot substitute for it.\n'
                '\n'
                'The V1 request that would be made:\n'
                f'  GET {{ACTIONS_CACHE_URL}}_apis/artifactcache/cache'
                f'?keys={key}&version=<sha256(paths|zstd|1.0)>\n'
                '  Authorization: Bearer <ACTIONS_RUNTIME_TOKEN>\n'
                '\n'
                'If you can supply those values (e.g. from inside a workflow job), re-run with\n'
                '  --cache-url <url> --runtime-token <token> --paths <path1> [<path2> ...]'
            ))
            return

        if not paths:
            raise CommandError(
                '--paths is required to compute the version hash. '
                'Check the workflow YAML for the `path:` input passed to actions/cache.'
            )

        version = compute_version(paths)
        self.stdout.write(f'Version hash : {version}')
        self.stdout.write(f'  (from paths: {paths}, compression: zstd, salt: 1.0)')
        self.stdout.write('')

        api_url = f'{cache_url.rstrip("/") + "/"}' \
                  f'_apis/artifactcache/cache' \
                  f'?keys={requests.utils.quote(key)}&version={version}'
        self.stdout.write(f'GET {api_url}')

        resp = requests.get(
            api_url,
            headers={
                'Authorization': f'Bearer {runtime_token}',
                'Accept': 'application/json;api-version=6.0-preview.1',
            },
            timeout=30,
        )

        self.stdout.write(f'Status: {resp.status_code}')

        if resp.status_code == 204:
            self.stdout.write(self.style.WARNING('Cache miss (204 No Content).'))
            return
        if not resp.ok:
            self.stdout.write(self.style.ERROR(f'Error: {resp.text}'))
            return

        data = resp.json()
        archive_location = data.get('archiveLocation')
        matched_key = data.get('cacheKey')
        self.stdout.write(self.style.SUCCESS(f'Cache hit! Matched key: {matched_key}'))
        self.stdout.write(f'Archive location: {archive_location}')

        # Download the archive.
        self.stdout.write('Downloading archive...')
        with requests.get(archive_location, stream=True, timeout=60) as dl:
            dl.raise_for_status()
            out_path = f'/tmp/{key}.tar.zst'
            total = 0
            with open(out_path, 'wb') as f:
                for chunk in dl.iter_content(chunk_size=1024 * 1024):
                    f.write(chunk)
                    total += len(chunk)
                    self.stdout.write(f'\r  {total / 1024 / 1024:.1f} MB', ending='')
                    sys.stdout.flush()
            self.stdout.write('')

        self.stdout.write(self.style.SUCCESS(f'Saved to {out_path} ({total:,} bytes)'))
