#!/usr/bin/env python3
"""
Download a GitHub Actions cache entry from outside a GitHub Actions workflow.

Usage:
    python download_cache.py \\
        --cache-url  "https://artifactcache.actions.githubusercontent.com/..." \\
        --token      "<ACTIONS_RUNTIME_TOKEN>" \\
        --key        "demo-cache-<run_id>-<run_attempt>" \\
        --paths      my-cached-dir \\
        --output-dir ./restored

The cache URL and token are printed by the workflow step
"Print ACTIONS_CACHE_URL and ACTIONS_RUNTIME_TOKEN". The token is printed
base64-encoded; decode it first:

    echo "<base64>" | base64 -d

Requires: requests   (pip install requests)
          zstd binary available on PATH for zstd-compressed caches (apt/brew install zstd)
"""

import argparse
import hashlib
import os
import subprocess
import sys
import tarfile
import tempfile

try:
    import requests
except ImportError:
    sys.exit("requests is required: pip install requests")


# ---------------------------------------------------------------------------
# Version calculation — mirrors @actions/cache getCacheVersion() / cacheUtils.ts
# ---------------------------------------------------------------------------

def compute_cache_version(
    paths: list[str],
    compression_method: str = "zstd",
    platform: str = "linux",
) -> str:
    """
    Replicates the TypeScript:

        const components = paths.slice();
        if (compressionMethod) components.push(compressionMethod);
        if (process.platform === 'win32' && !enableCrossOsArchive)
            components.push('windows-only');
        components.push(versionSalt);   // '1.0'
        return crypto.createHash('sha256').update(components.join('|')).digest('hex');

    Args:
        paths:              The list of paths passed to actions/cache (the `path:` input).
        compression_method: "zstd" (Linux/macOS default), "zstd-without-long", or "gzip".
        platform:           "linux", "macos", or "windows".
    """
    components = list(paths)
    if compression_method:
        components.append(compression_method)
    if platform == "windows":
        components.append("windows-only")
    components.append("1.0")  # versionSalt
    joined = "|".join(components)
    version = hashlib.sha256(joined.encode()).hexdigest()
    return version


# ---------------------------------------------------------------------------
# Cache API calls  (v1 API: ACTIONS_CACHE_URL + _apis/artifactcache/...)
# ---------------------------------------------------------------------------

def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json;api-version=6.0-preview.1",
    }


def get_cache_entry(
    cache_url: str,
    token: str,
    key: str,
    version: str,
) -> dict | None:
    """
    GET {cache_url}_apis/artifactcache/cache?keys={key}&version={version}

    Returns the JSON body on a cache hit (200), None on a cache miss (204).
    Raises for any other status.

    Successful response shape:
        {
            "cacheKey": "demo-cache-42-1",
            "cacheVersion": "<sha256>",
            "creationTime": "2025-01-01T00:00:00Z",
            "archiveLocation": "https://..."   # pre-signed download URL
        }
    """
    if not cache_url.endswith("/"):
        cache_url += "/"
    url = f"{cache_url}_apis/artifactcache/cache"
    resp = requests.get(
        url,
        headers=_headers(token),
        params={"keys": key, "version": version},
        timeout=30,
    )
    if resp.status_code == 204:
        return None  # cache miss
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Download + extract
# ---------------------------------------------------------------------------

def download_archive(archive_location: str, dest_path: str) -> None:
    """Stream the cache archive from the pre-signed archiveLocation URL."""
    with requests.get(archive_location, stream=True, timeout=300) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("Content-Length", 0))
        downloaded = 0
        with open(dest_path, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=65536):
                fh.write(chunk)
                downloaded += len(chunk)
        if total:
            print(f"  Downloaded {downloaded:,} / {total:,} bytes")
        else:
            print(f"  Downloaded {downloaded:,} bytes")


def extract_archive(
    archive_path: str,
    output_dir: str,
    compression_method: str,
) -> None:
    """Extract the tar archive (zstd or gzip) into output_dir."""
    os.makedirs(output_dir, exist_ok=True)

    if compression_method in ("zstd", "zstd-without-long"):
        # tarfile doesn't speak zstd; delegate to the system tar + zstd.
        try:
            subprocess.run(
                ["tar", "--use-compress-program=zstd -d", "-xf", archive_path, "-C", output_dir],
                check=True,
            )
        except FileNotFoundError:
            sys.exit(
                "ERROR: 'tar' or 'zstd' not found. "
                "Install zstd (apt install zstd / brew install zstd) and retry."
            )
    elif compression_method == "gzip":
        with tarfile.open(archive_path, "r:gz") as tar:
            tar.extractall(output_dir)
    else:
        raise ValueError(f"Unknown compression method: {compression_method!r}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download a GitHub Actions cache entry without being inside a workflow.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--cache-url", required=True,
        help="Value of ACTIONS_CACHE_URL printed by the workflow (ends with '/').",
    )
    parser.add_argument(
        "--token", required=True,
        help="Value of ACTIONS_RUNTIME_TOKEN (decode from base64 first if needed).",
    )
    parser.add_argument(
        "--key", required=True,
        help="Exact cache key used in the actions/cache step.",
    )
    parser.add_argument(
        "--paths", required=True, nargs="+",
        help="The path(s) listed under `path:` in the actions/cache step.",
    )
    parser.add_argument(
        "--compression", default="zstd",
        choices=["zstd", "zstd-without-long", "gzip"],
        help="Compression method used when the cache was created (default: zstd on Linux/macOS).",
    )
    parser.add_argument(
        "--platform", default="linux",
        choices=["linux", "macos", "windows"],
        help="Platform the cache was created on — affects version hash (default: linux).",
    )
    parser.add_argument(
        "--output-dir", default="restored-cache",
        help="Directory to extract the cache archive into (default: restored-cache).",
    )
    parser.add_argument(
        "--keep-archive", action="store_true",
        help="Keep the downloaded archive file instead of deleting it after extraction.",
    )
    args = parser.parse_args()

    # 1. Compute the cache version (must match what the action computed).
    version = compute_cache_version(args.paths, args.compression, args.platform)
    print(f"Cache version : {version}")
    print(f"Cache key     : {args.key}")
    print(f"Paths         : {args.paths}")
    print(f"Compression   : {args.compression}")
    print()

    # 2. Look up the cache entry.
    print("Querying cache service…")
    entry = get_cache_entry(args.cache_url, args.token, args.key, version)
    if entry is None:
        print("Cache MISS — no entry found for this key + version.")
        sys.exit(1)

    print(f"Cache HIT!")
    print(f"  cacheKey     : {entry.get('cacheKey')}")
    print(f"  cacheVersion : {entry.get('cacheVersion')}")
    print(f"  creationTime : {entry.get('creationTime')}")
    archive_url = entry["archiveLocation"]
    print()

    # 3. Download the archive to a temp file.
    suffix = ".tar.zst" if args.compression != "gzip" else ".tar.gz"
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    os.close(tmp_fd)
    try:
        print(f"Downloading archive…")
        download_archive(archive_url, tmp_path)

        # 4. Extract.
        print(f"Extracting into {args.output_dir!r}…")
        extract_archive(tmp_path, args.output_dir, args.compression)
        print("Done.")

        if args.keep_archive:
            print(f"Archive kept at: {tmp_path}")
            tmp_path = None  # don't delete
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


if __name__ == "__main__":
    main()
