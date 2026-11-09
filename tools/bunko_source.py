"""Download and verify package and toolchain bytes from GitHub.

The checks mirror docs/package-format.md: the release must be published and
immutable, the asset must belong to it, the release tag must resolve to the
recorded source commit, and the downloaded bytes must match the recorded
SHA-256. Credentials are only ever sent to api.github.com, never to redirect
targets.
"""

import hashlib
import json
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request

API_HOST = "api.github.com"
API = f"https://{API_HOST}"
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_TOOLCHAIN_BYTES = 128 * 1024 * 1024
MAX_JSON_BYTES = 4 * 1024 * 1024
TIMEOUT = 60
ASSET_ID = re.compile(r"https://api\.github\.com/repos/([a-z0-9-]+)/([A-Za-z0-9_.-]+)/releases/assets/([1-9][0-9]{0,19})")
REPOSITORY = re.compile(r"https://github\.com/([a-z0-9-]+)/([A-Za-z0-9_.-]+)")
DOWNLOAD_HOSTS = ("github.com", ".githubusercontent.com")


class SourceError(ValueError):
    """Release, provenance or checksum verification failed."""

    code = "BUNKO_E_SOURCE_VERIFY"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _allowed_download_host(url):
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    return parsed.scheme == "https" and parsed.username is None and parsed.port in (None, 443) and (
        host == DOWNLOAD_HOSTS[0] or host.endswith(DOWNLOAD_HOSTS[1]))


class Fetcher:
    """HTTPS client with manual redirects so credentials never leave api.github.com."""

    def __init__(self, token=None, sleep=time.sleep):
        self.token = token
        self.sleep = sleep
        self.opener = urllib.request.build_opener(_NoRedirect)

    def _open(self, url, accept, authenticate):
        headers = {"Accept": accept, "User-Agent": "bunko-index-ci", "X-GitHub-Api-Version": "2022-11-28"}
        if authenticate and self.token and urllib.parse.urlsplit(url).hostname == API_HOST:
            headers["Authorization"] = f"Bearer {self.token}"
        return self.opener.open(urllib.request.Request(url, headers=headers), timeout=TIMEOUT)

    def _request(self, url, accept, authenticate=True):
        """Follow up to three redirects to allowed HTTPS hosts. Retry transient failures."""
        last = None
        for attempt in range(3):
            current = url
            try:
                for _ in range(4):
                    try:
                        return self._open(current, accept, authenticate)
                    except urllib.error.HTTPError as error:
                        if error.code in (301, 302, 303, 307, 308) and error.headers.get("Location"):
                            current = urllib.parse.urljoin(current, error.headers["Location"])
                            if not _allowed_download_host(current) and urllib.parse.urlsplit(current).hostname != API_HOST:
                                raise SourceError(f"refusing redirect to unexpected host for {url}") from None
                            continue
                        if error.code in (500, 502, 503, 504):
                            last = error
                            break
                        raise SourceError(f"HTTP {error.code} fetching {url}") from None
                else:
                    raise SourceError(f"too many redirects fetching {url}")
            except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
                last = error
            self.sleep(2 ** attempt)
        raise SourceError(f"could not fetch {url}: {last}")

    def json(self, url):
        if urllib.parse.urlsplit(url).hostname != API_HOST:
            raise SourceError(f"JSON is only fetched from {API_HOST}")
        with self._request(url, "application/vnd.github+json") as response:
            body = response.read(MAX_JSON_BYTES + 1)
        if len(body) > MAX_JSON_BYTES:
            raise SourceError(f"response from {url} is too large")
        try:
            return json.loads(body)
        except ValueError:
            raise SourceError(f"response from {url} is not JSON") from None

    def download(self, url, destination, max_bytes):
        """Stream url to destination and return (sha256 hex digest, size)."""
        digest = hashlib.sha256()
        size = 0
        with self._request(url, "application/octet-stream") as response, open(destination, "wb") as out:
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise SourceError(f"download exceeds {max_bytes} bytes: {url}")
                digest.update(chunk)
                out.write(chunk)
        return digest.hexdigest(), size


def _require(condition, message):
    if not condition:
        raise SourceError(message)


def verify_release(record, fetcher):
    """Check the immutable release, asset identity and source commit for one record."""
    repository = REPOSITORY.fullmatch(record["repository"])
    archive = ASSET_ID.fullmatch(record["archive"])
    _require(repository and archive and repository.groups() == archive.groups()[:2],
             "repository and archive do not describe the same GitHub repository")
    owner, repo = repository.groups()
    label = f"{record['name']}@{record['version']}"
    release = fetcher.json(f"{API}/repos/{owner}/{repo}/releases/{record['release_id']}")
    _require(isinstance(release, dict) and str(release.get("id")) == record["release_id"],
             f"{label}: release {record['release_id']} not found in {owner}/{repo}")
    _require(release.get("draft") is False, f"{label}: release is a draft")
    _require(release.get("immutable") is True,
             f"{label}: release is not immutable; enable immutable releases and publish a new version")
    asset_id = int(archive[3])
    assets = [a for a in release.get("assets", []) if isinstance(a, dict) and a.get("id") == asset_id]
    _require(len(assets) == 1, f"{label}: asset {asset_id} is not part of release {record['release_id']}")
    asset = assets[0]
    _require(asset.get("url") == record["archive"], f"{label}: asset URL differs from the recorded archive")
    _require(str(asset.get("name", "")).endswith(".tar.gz"), f"{label}: asset is not a .tar.gz archive")
    _require(asset.get("state", "uploaded") == "uploaded", f"{label}: asset is not fully uploaded")
    size = asset.get("size")
    _require(isinstance(size, int) and 0 < size <= MAX_ARCHIVE_BYTES,
             f"{label}: asset size is outside 1..{MAX_ARCHIVE_BYTES} bytes")
    digest = asset.get("digest")
    _require(digest in (None, f"sha256:{record['sha256']}"),
             f"{label}: GitHub reports digest {digest}, but the record says sha256:{record['sha256']}")
    tag = release.get("tag_name")
    _require(isinstance(tag, str) and tag, f"{label}: release has no tag")
    _require(resolve_tag(fetcher, owner, repo, tag) == record["source_commit"],
             f"{label}: tag {tag!r} does not resolve to source_commit {record['source_commit']}")
    return size


def resolve_tag(fetcher, owner, repo, tag):
    """Resolve a tag, peeling annotated tag objects, to a commit SHA."""
    reference = fetcher.json(f"{API}/repos/{owner}/{repo}/git/ref/tags/{urllib.parse.quote(tag, safe='/')}")
    target = reference.get("object", {}) if isinstance(reference, dict) else {}
    for _ in range(3):
        sha, kind = target.get("sha"), target.get("type")
        if kind == "commit":
            return sha
        if kind != "tag" or not isinstance(sha, str):
            break
        target = fetcher.json(f"{API}/repos/{owner}/{repo}/git/tags/{sha}").get("object", {})
    raise SourceError(f"tag {tag!r} does not point to a commit")


def fetch_package_archive(record, fetcher, destination):
    """Verify the release, download the asset and check its SHA-256."""
    verify_release(record, fetcher)
    digest, _ = fetcher.download(record["archive"], destination, MAX_ARCHIVE_BYTES)
    if digest != record["sha256"]:
        Path(destination).unlink(missing_ok=True)
        raise SourceError(
            f"{record['name']}@{record['version']}: archive SHA-256 is {digest}, record says {record['sha256']}")


def fetch_toolchain(rule, fetcher, destination):
    """Download the pinned official Mio release asset and verify its pinned SHA-256."""
    url = f"https://github.com/mio-kobo/mio/releases/download/v{rule['mio_version']}/{rule['mio_asset']}"
    digest, _ = fetcher.download(url, destination, MAX_TOOLCHAIN_BYTES)
    if digest != rule["mio_sha256"]:
        Path(destination).unlink(missing_ok=True)
        raise SourceError(f"{rule['mio_asset']}: SHA-256 is {digest}, pinned value is {rule['mio_sha256']}")
