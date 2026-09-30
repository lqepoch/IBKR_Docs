from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "sync_ibkr_docs.py"
SPEC = importlib.util.spec_from_file_location("sync_ibkr_docs", MODULE_PATH)
assert SPEC and SPEC.loader
sync = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sync
SPEC.loader.exec_module(sync)


class NormalizeUrlTests(unittest.TestCase):
    def test_absolute_page(self) -> None:
        self.assertEqual(
            sync.normalize_page_url(
                "https://ibkrcampus.com/docs/tws-api/doc/introduction#section"
            ),
            "https://ibkrcampus.com/docs/tws-api/doc/introduction",
        )

    def test_relative_page(self) -> None:
        self.assertEqual(
            sync.normalize_page_url("/docs/tws-api/ref/contract.md"),
            "https://ibkrcampus.com/docs/tws-api/ref/contract",
        )

    def test_out_of_scope(self) -> None:
        self.assertIsNone(
            sync.normalize_page_url("https://example.com/docs/tws-api/ref/order")
        )
        self.assertIsNone(sync.normalize_page_url("/docs/web-api/ref/order"))

    def test_non_page_asset(self) -> None:
        self.assertIsNone(
            sync.normalize_page_url("/docs/tws-api/assets/diagram.png")
        )
        self.assertIsNone(sync.normalize_page_url("/docs/tws-api/llms.txt"))


class LinkExtractionTests(unittest.TestCase):
    def test_extracts_markdown_absolute_and_raw_paths(self) -> None:
        text_value = """
        [Intro](/docs/tws-api/doc/introduction)
        https://ibkrcampus.com/docs/tws-api/ref/contract.md
        /docs/tws-api/protobuf/introduction
        https://example.com/not-in-scope
        """
        self.assertEqual(
            sync.extract_in_scope_links(text_value),
            {
                "https://ibkrcampus.com/docs/tws-api/doc/introduction",
                "https://ibkrcampus.com/docs/tws-api/ref/contract",
                "https://ibkrcampus.com/docs/tws-api/protobuf/introduction",
            },
        )

    def test_relative_link_uses_page_base(self) -> None:
        self.assertEqual(
            sync.extract_in_scope_links(
                "[Contract](../ref/contract)",
                base_url="https://ibkrcampus.com/docs/tws-api/doc/introduction",
            ),
            {"https://ibkrcampus.com/docs/tws-api/ref/contract"},
        )

    def test_local_path_preserves_upstream_tree(self) -> None:
        self.assertEqual(
            sync.local_relative_path(
                "https://ibkrcampus.com/docs/tws-api/doc/market-data/historical-data"
            ).as_posix(),
            "docs/tws-api/doc/market-data/historical-data.md",
        )


class ManifestTests(unittest.TestCase):
    def test_manifest_is_deterministic(self) -> None:
        documents = [
            {
                "path": "docs/tws-api/ref/z.md",
                "url": "u2",
                "markdown_url": "m2",
                "sha256": "b",
                "bytes": 2,
            },
            {
                "path": "docs/tws-api/doc/a.md",
                "url": "u1",
                "markdown_url": "m1",
                "sha256": "a",
                "bytes": 1,
            },
        ]
        manifest = sync.deterministic_manifest(documents)
        self.assertEqual(
            [item["path"] for item in manifest["documents"]],
            ["docs/tws-api/doc/a.md", "docs/tws-api/ref/z.md"],
        )


if __name__ == "__main__":
    unittest.main()
