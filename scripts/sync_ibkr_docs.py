#!/usr/bin/env python3
"""Mirror official IBKR TWS API documentation into this repository.

The local layout preserves the official documentation URL path:

    https://www.interactivebrokers.com/docs/tws-api/doc/introduction
    -> docs/tws-api/doc/introduction.md

The synchronizer prefers IBKR's official Markdown representation when it is
reachable. If that representation is unavailable from the CI network, it
fetches the canonical official HTML page and deterministically converts the
article content to Markdown.

Publication is fail-closed: pages are staged, coverage and hashes are checked,
and only a validated working-tree snapshot is eligible for commit.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import posixpath
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from bs4 import BeautifulSoup
from markdownify import markdownify as html_to_markdown

BASE_URL = "https://www.interactivebrokers.com"
UPSTREAM_PREFIX = "/docs/tws-api"
UPSTREAM_ROOT = BASE_URL + UPSTREAM_PREFIX

ALLOWED_HOSTS = {
    "interactivebrokers.com",
    "www.interactivebrokers.com",
    "ibkrcampus.com",
    "www.ibkrcampus.com",
}

REPO_ROOT = Path(__file__).resolve().parents[1]
MIRROR_ROOT = REPO_ROOT / "docs" / "tws-api"
META_ROOT = REPO_ROOT / ".meta"
MANIFEST_PATH = META_ROOT / "manifest.json"
CATALOG_PATH = META_ROOT / "catalog.txt"
SOURCE_PATH = META_ROOT / "source.json"

INDEX_URLS = (
    f"{BASE_URL}/llms.txt",
)

SEED_PAGES = (
    f"{UPSTREAM_ROOT}/doc/introduction",
    f"{UPSTREAM_ROOT}/ref/introduction",
    f"{UPSTREAM_ROOT}/protobuf/introduction",
)

NON_PAGE_SUFFIXES = {
    ".css",
    ".gif",
    ".ico",
    ".jpeg",
    ".jpg",
    ".js",
    ".json",
    ".pdf",
    ".png",
    ".svg",
    ".txt",
    ".webp",
    ".xml",
    ".yaml",
    ".yml",
    ".zip",
}

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0 Safari/537.36 "
    "IBKR_Docs-Mirror/2.0"
)

DEFAULT_TIMEOUT = 30.0
DEFAULT_DELAY = 0.05
DEFAULT_MAX_PAGES = 5000
DEFAULT_MIN_PAGES = 50
DEFAULT_MAX_REMOVAL_RATIO = 0.15


class SyncError(RuntimeError):
    """Raised when the mirror cannot be updated safely."""


@dataclass(frozen=True)
class FetchResult:
    url: str
    text: str
    content_type: str


@dataclass(frozen=True)
class DocumentResult:
    markdown: str
    source_url: str
    source_format: str
    source_sha256: str
    links: frozenset[str]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def retry_delay(attempt: int, retry_after: str | None) -> float:
    if retry_after:
        try:
            return min(max(float(retry_after), 1.0), 60.0)
        except ValueError:
            pass
    return min(2.0**attempt, 30.0)


def fetch_text(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = 12 * 1024 * 1024,
    attempts: int = 5,
) -> FetchResult:
    """Fetch text with bounded retries, size limits, and redirect validation."""

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/markdown,text/plain;q=0.9,text/html;q=0.8,*/*;q=0.1",
        "Accept-Encoding": "identity",
        "Cache-Control": "no-cache",
    }
    last_error: BaseException | None = None

    for attempt in range(attempts):
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                final_url = response.geturl()
                host = (urllib.parse.urlsplit(final_url).hostname or "").lower()
                if host not in ALLOWED_HOSTS:
                    raise SyncError(
                        f"unexpected redirect host for {url}: {host or '<empty>'}"
                    )

                payload = response.read(max_bytes + 1)
                if len(payload) > max_bytes:
                    raise SyncError(f"response exceeds {max_bytes} bytes: {url}")

                charset = response.headers.get_content_charset() or "utf-8"
                text = payload.decode(charset, errors="replace")
                content_type = response.headers.get_content_type() or ""
                return FetchResult(final_url, text, content_type)

        except urllib.error.HTTPError as exc:
            if exc.code in {403, 404, 410}:
                raise
            last_error = exc
            if exc.code not in {408, 425, 429, 500, 502, 503, 504}:
                raise
            time.sleep(retry_delay(attempt, exc.headers.get("Retry-After")))
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_error = exc
            time.sleep(retry_delay(attempt, None))

    raise SyncError(f"failed after {attempts} attempts: {url}: {last_error}")


def normalize_page_url(
    raw: str,
    *,
    base_url: str = UPSTREAM_ROOT + "/",
) -> str | None:
    """Normalize an in-scope documentation URL to the canonical current host."""

    value = html.unescape(raw.strip()).strip("<>")
    value = value.strip(chr(34)).strip(chr(39))
    if not value or value.startswith(("#", "mailto:", "javascript:", "data:")):
        return None

    joined = urllib.parse.urljoin(base_url, value)
    parsed = urllib.parse.urlsplit(joined)
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        return None

    path = urllib.parse.unquote(parsed.path)
    path = posixpath.normpath("/" + path.lstrip("/"))

    if path != UPSTREAM_PREFIX and not path.startswith(UPSTREAM_PREFIX + "/"):
        return None

    if path.endswith("/llms.txt") or path == UPSTREAM_PREFIX + "/llms.txt":
        return None

    if path.endswith(".md"):
        path = path[:-3]

    suffix = Path(path).suffix.lower()
    if suffix in NON_PAGE_SUFFIXES:
        return None

    if path == UPSTREAM_PREFIX:
        return None

    return BASE_URL + path.rstrip("/")


def markdown_link_target(value: str) -> str:
    """Strip an optional Markdown link title."""

    target = value.strip()
    if target.startswith("<") and ">" in target:
        return target[1 : target.index(">")]

    for marker in (' "', " '"):
        if marker in target:
            return target.split(marker, 1)[0]
    return target


def extract_markdown_links(
    text: str,
    *,
    base_url: str = UPSTREAM_ROOT + "/",
) -> set[str]:
    candidates: set[str] = set()

    for match in re.finditer(r"\[[^\]]*\]\(([^)]+)\)", text):
        candidates.add(markdown_link_target(match.group(1)))

    candidates.update(
        match.rstrip(".,;:)]}>")
        for match in re.findall(r"https?://[^\s<>\"']+", text)
    )
    candidates.update(
        match.rstrip(".,;:)]}>")
        for match in re.findall(r"/docs/tws-api/[A-Za-z0-9_./%+\-]+", text)
    )

    result: set[str] = set()
    for candidate in candidates:
        page = normalize_page_url(candidate, base_url=base_url)
        if page:
            result.add(page)
    return result


def parse_html(html_text: str) -> BeautifulSoup:
    return BeautifulSoup(html_text, "html.parser")


def extract_html_links(
    html_text: str,
    *,
    base_url: str,
) -> set[str]:
    soup = parse_html(html_text)
    result: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        page = normalize_page_url(str(anchor.get("href")), base_url=base_url)
        if page:
            result.add(page)
    return result


def render_html_document(html_text: str, *, source_url: str) -> str:
    """Extract the documentation article and convert it to deterministic Markdown."""

    soup = parse_html(html_text)
    container = soup.find("article")
    if container is None:
        container = soup.find("main")
    if container is None:
        raise SyncError(f"cannot locate article/main content in {source_url}")

    for element in container.select(
        "script,style,noscript,nav,aside,footer,header,form,button,svg"
    ):
        element.decompose()

    rendered = html_to_markdown(
        str(container),
        heading_style="ATX",
        bullets="-",
        strip=["img"],
    ).strip()

    rendered = re.sub(r"\n[ \t]+\n", "\n\n", rendered)
    rendered = re.sub(r"\n{4,}", "\n\n\n", rendered)

    if len(rendered) < 80:
        raise SyncError(f"converted article is unexpectedly short: {source_url}")

    header = (
        "<!-- AUTO-GENERATED IBKR DOCUMENTATION CACHE. DO NOT EDIT. -->\n"
        f"<!-- Source: {source_url} -->\n"
        "<!-- Source format: official HTML converted to Markdown. -->\n\n"
    )
    return header + rendered + "\n"


def looks_like_markdown(text: str, content_type: str) -> bool:
    stripped = text.lstrip()
    prefix = stripped[:512].lower()
    if not stripped:
        return False
    if "<html" in prefix or "<!doctype html" in prefix:
        return False
    if content_type == "text/html":
        return False
    return True


def markdown_url(canonical_url: str) -> str:
    return canonical_url.rstrip("/") + ".md"


def detect_official_markdown(*, timeout: float) -> bool:
    """Probe once so a blocked Markdown host does not double every page request."""

    probe = markdown_url(SEED_PAGES[0])
    try:
        result = fetch_text(probe, timeout=timeout, attempts=2)
    except Exception:
        return False
    return looks_like_markdown(result.text, result.content_type)


def fetch_document(
    canonical_url: str,
    *,
    timeout: float,
    prefer_markdown: bool,
) -> DocumentResult:
    if prefer_markdown:
        try:
            result = fetch_text(markdown_url(canonical_url), timeout=timeout)
            if looks_like_markdown(result.text, result.content_type):
                return DocumentResult(
                    markdown=result.text.rstrip() + "\n",
                    source_url=result.url,
                    source_format="official_markdown",
                    source_sha256=sha256_text(result.text),
                    links=frozenset(
                        extract_markdown_links(result.text, base_url=canonical_url)
                    ),
                )
        except Exception:
            pass

    result = fetch_text(canonical_url, timeout=timeout)
    links = extract_html_links(result.text, base_url=canonical_url)
    markdown = render_html_document(result.text, source_url=canonical_url)
    return DocumentResult(
        markdown=markdown,
        source_url=result.url,
        source_format="html_to_markdown",
        source_sha256=sha256_text(result.text),
        links=frozenset(links),
    )


def local_relative_path(canonical_url: str) -> Path:
    parsed = urllib.parse.urlsplit(canonical_url)
    path = posixpath.normpath("/" + parsed.path.lstrip("/"))
    if not path.startswith(UPSTREAM_PREFIX + "/"):
        raise SyncError(f"URL is outside mirror scope: {canonical_url}")

    relative = Path(path.lstrip("/") + ".md")
    if relative.is_absolute() or ".." in relative.parts:
        raise SyncError(f"unsafe local path from {canonical_url}: {relative}")
    return relative


def load_manifest(path: Path = MANIFEST_PATH) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SyncError(f"cannot parse existing manifest {path}: {exc}") from exc


def manifest_hash_map(manifest: dict | None) -> dict[str, str]:
    if not manifest:
        return {}

    result: dict[str, str] = {}
    for item in manifest.get("documents", []):
        path = item.get("path")
        digest = item.get("sha256")
        if isinstance(path, str) and isinstance(digest, str):
            result[path] = digest
    return result


def category_counts(documents: Iterable[dict]) -> dict[str, int]:
    counts = {"doc": 0, "ref": 0, "protobuf": 0, "other": 0}
    for item in documents:
        path = item["path"]
        if path.startswith("docs/tws-api/doc/"):
            counts["doc"] += 1
        elif path.startswith("docs/tws-api/ref/"):
            counts["ref"] += 1
        elif path.startswith("docs/tws-api/protobuf/"):
            counts["protobuf"] += 1
        else:
            counts["other"] += 1
    return counts


def validate_thresholds(documents: list[dict], minimum_pages: int) -> None:
    if len(documents) < minimum_pages:
        raise SyncError(
            f"discovered only {len(documents)} pages; safety minimum is {minimum_pages}"
        )

    counts = category_counts(documents)
    required = {"doc": 10, "ref": 10, "protobuf": 3}
    failures = [
        f"{name}={counts[name]} (<{minimum})"
        for name, minimum in required.items()
        if counts[name] < minimum
    ]
    if failures:
        raise SyncError("category completeness gate failed: " + ", ".join(failures))


def validate_removal_guard(
    old_manifest: dict | None,
    new_manifest: dict,
    *,
    max_removal_ratio: float,
    allow_mass_removal: bool,
) -> None:
    old = manifest_hash_map(old_manifest)
    new = manifest_hash_map(new_manifest)
    if not old:
        return

    removed = set(old) - set(new)
    hard_limit = max(5, int(len(old) * max_removal_ratio))
    if len(removed) > hard_limit and not allow_mass_removal:
        ratio = len(removed) / len(old)
        sample = ", ".join(sorted(removed)[:5])
        raise SyncError(
            "mass-removal guard blocked publication: "
            f"{len(removed)}/{len(old)} pages ({ratio:.1%}) would be removed; "
            f"sample: {sample}. Set IBKR_DOCS_ALLOW_MASS_REMOVAL=1 only after "
            "manually verifying an upstream information-architecture migration."
        )


def deterministic_manifest(documents: list[dict]) -> dict:
    docs = sorted(documents, key=lambda item: item["path"])
    return {
        "schema_version": 2,
        "source": UPSTREAM_ROOT,
        "mirror_root": "docs/tws-api",
        "document_count": len(docs),
        "category_counts": category_counts(docs),
        "documents": docs,
    }


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def publish_stage(stage_root: Path, manifest: dict) -> None:
    stage_mirror = stage_root / "docs" / "tws-api"
    if not stage_mirror.is_dir():
        raise SyncError("staged mirror directory is missing")

    if MIRROR_ROOT.exists():
        shutil.rmtree(MIRROR_ROOT)
    MIRROR_ROOT.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(stage_mirror), str(MIRROR_ROOT))

    write_json(MANIFEST_PATH, manifest)
    CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CATALOG_PATH.write_text(
        "".join(item["path"] + "\n" for item in manifest["documents"]),
        encoding="utf-8",
    )
    write_json(
        SOURCE_PATH,
        {
            "schema_version": 2,
            "upstream": UPSTREAM_ROOT,
            "canonical_host": BASE_URL,
            "discovery_indexes": list(INDEX_URLS),
            "storage_rule": (
                "Preserve the official /docs/tws-api URL path under the "
                "repository root and append .md"
            ),
            "fallback_rule": (
                "Prefer official Markdown; when unavailable, convert the "
                "canonical official HTML article to Markdown"
            ),
        },
    )


def bootstrap_discovery(*, timeout: float) -> tuple[set[str], list[dict[str, str]]]:
    """Discover the full navigation tree from canonical HTML and optional indexes."""

    discovered: set[str] = set(SEED_PAGES)
    warnings: list[dict[str, str]] = []

    for index_url in INDEX_URLS:
        try:
            result = fetch_text(index_url, timeout=timeout)
            discovered.update(
                extract_markdown_links(result.text, base_url=index_url)
            )
        except Exception as exc:
            warnings.append({"url": index_url, "reason": str(exc)})

    successful_seed_html = 0
    for seed in SEED_PAGES:
        try:
            result = fetch_text(seed, timeout=timeout)
            successful_seed_html += 1
            discovered.update(extract_html_links(result.text, base_url=seed))
        except Exception as exc:
            warnings.append({"url": seed, "reason": str(exc)})

    if successful_seed_html == 0:
        raise SyncError("none of the canonical TWS API seed pages could be fetched")

    return discovered, warnings


def sync(
    *,
    timeout: float,
    request_delay: float,
    max_pages: int,
    minimum_pages: int,
    max_removal_ratio: float,
) -> dict:
    discovered, discovery_warnings = bootstrap_discovery(timeout=timeout)
    prefer_markdown = detect_official_markdown(timeout=timeout)

    queue = deque(sorted(discovered))
    queued = set(queue)
    documents: list[dict] = []
    stale_pages: list[dict[str, str]] = []

    with tempfile.TemporaryDirectory(
        prefix=".ibkr-docs-stage-",
        dir=REPO_ROOT,
    ) as temp_dir:
        stage_root = Path(temp_dir)

        while queue:
            if len(documents) + len(stale_pages) >= max_pages:
                raise SyncError(
                    f"page limit {max_pages} reached; refusing a partial mirror"
                )

            canonical_url = queue.popleft()
            relative_path = local_relative_path(canonical_url)

            try:
                document = fetch_document(
                    canonical_url,
                    timeout=timeout,
                    prefer_markdown=prefer_markdown,
                )
            except urllib.error.HTTPError as exc:
                if exc.code in {403, 404, 410}:
                    stale_pages.append(
                        {"url": canonical_url, "reason": f"HTTP {exc.code}"}
                    )
                    continue
                raise
            except Exception as exc:
                raise SyncError(
                    f"failed to fetch document {canonical_url}: {exc}"
                ) from exc

            target = stage_root / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(document.markdown, encoding="utf-8")

            documents.append(
                {
                    "path": relative_path.as_posix(),
                    "url": canonical_url,
                    "official_markdown_url": markdown_url(canonical_url),
                    "source_url": document.source_url,
                    "source_format": document.source_format,
                    "source_sha256": document.source_sha256,
                    "sha256": sha256_text(document.markdown),
                    "bytes": len(document.markdown.encode("utf-8")),
                }
            )

            for linked in sorted(document.links):
                if linked not in queued:
                    queued.add(linked)
                    queue.append(linked)

            if request_delay > 0:
                time.sleep(request_delay)

        validate_thresholds(documents, minimum_pages)
        manifest = deterministic_manifest(documents)
        old_manifest = load_manifest()

        validate_removal_guard(
            old_manifest,
            manifest,
            max_removal_ratio=max_removal_ratio,
            allow_mass_removal=os.environ.get(
                "IBKR_DOCS_ALLOW_MASS_REMOVAL"
            ) == "1",
        )

        changed = manifest_hash_map(old_manifest) != manifest_hash_map(manifest)
        if changed:
            publish_stage(stage_root, manifest)

    return {
        "changed": changed,
        "document_count": len(documents),
        "category_counts": manifest["category_counts"],
        "preferred_source_format": (
            "official_markdown" if prefer_markdown else "html_to_markdown"
        ),
        "discovery_warnings": discovery_warnings,
        "stale_pages": stale_pages,
    }


def validate_existing(*, minimum_pages: int = DEFAULT_MIN_PAGES) -> dict:
    manifest = load_manifest()
    if not manifest:
        raise SyncError(f"manifest is missing: {MANIFEST_PATH}")

    documents = manifest.get("documents")
    if not isinstance(documents, list):
        raise SyncError("manifest documents must be a list")

    validate_thresholds(documents, minimum_pages)
    expected_paths: set[str] = set()

    for item in documents:
        relative = item.get("path")
        expected_sha = item.get("sha256")
        expected_bytes = item.get("bytes")

        if not isinstance(relative, str) or not relative.startswith(
            "docs/tws-api/"
        ):
            raise SyncError(f"unsafe or invalid manifest path: {relative!r}")

        path = REPO_ROOT / relative
        if path.is_symlink():
            raise SyncError(f"symlink is not allowed in mirror: {relative}")
        if not path.is_file():
            raise SyncError(f"mirrored file is missing: {relative}")

        text = path.read_text(encoding="utf-8")
        actual_sha = sha256_text(text)
        actual_bytes = len(text.encode("utf-8"))

        if actual_sha != expected_sha:
            raise SyncError(f"SHA-256 mismatch: {relative}")
        if actual_bytes != expected_bytes:
            raise SyncError(f"byte-count mismatch: {relative}")

        expected_paths.add(relative)

    actual_paths = {
        path.relative_to(REPO_ROOT).as_posix()
        for path in MIRROR_ROOT.rglob("*.md")
        if path.is_file()
    }
    extras = actual_paths - expected_paths
    missing = expected_paths - actual_paths
    if extras or missing:
        raise SyncError(
            "manifest/file-set mismatch: "
            f"extras={sorted(extras)[:5]}, missing={sorted(missing)[:5]}"
        )

    if manifest.get("document_count") != len(documents):
        raise SyncError(
            "manifest document_count does not match the document list length"
        )

    return {
        "valid": True,
        "document_count": len(documents),
        "category_counts": category_counts(documents),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync_parser = subparsers.add_parser(
        "sync",
        help="download and stage the official mirror",
    )
    sync_parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    sync_parser.add_argument(
        "--request-delay",
        type=float,
        default=DEFAULT_DELAY,
    )
    sync_parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    sync_parser.add_argument(
        "--minimum-pages",
        type=int,
        default=DEFAULT_MIN_PAGES,
    )
    sync_parser.add_argument(
        "--max-removal-ratio",
        type=float,
        default=DEFAULT_MAX_REMOVAL_RATIO,
    )

    validate_parser = subparsers.add_parser(
        "validate",
        help="validate the committed mirror",
    )
    validate_parser.add_argument(
        "--minimum-pages",
        type=int,
        default=DEFAULT_MIN_PAGES,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        if args.command == "sync":
            result = sync(
                timeout=args.timeout,
                request_delay=args.request_delay,
                max_pages=args.max_pages,
                minimum_pages=args.minimum_pages,
                max_removal_ratio=args.max_removal_ratio,
            )
        else:
            result = validate_existing(minimum_pages=args.minimum_pages)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
