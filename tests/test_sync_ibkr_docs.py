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
    def test_absolute_current_page(self) -> None:
        self.assertEqual(
            sync.normalize_page_url(
                "https://www.interactivebrokers.com/docs/tws-api/doc/introduction#section"
            ),
            "https://www.interactivebrokers.com/docs/tws-api/doc/introduction",
        )

    def test_legacy_host_is_canonicalized(self) -> None:
        self.assertEqual(
            sync.normalize_page_url(
                "https://ibkrcampus.com/docs/tws-api/ref/contract.md"
            ),
            "https://www.interactivebrokers.com/docs/tws-api/ref/contract",
        )

    def test_relative_page(self) -> None:
        self.assertEqual(
            sync.normalize_page_url("/docs/tws-api/ref/contract.md"),
            "https://www.interactivebrokers.com/docs/tws-api/ref/contract",
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
    def test_extracts_markdown_links(self) -> None:
        text_value = """
        [Intro](/docs/tws-api/doc/introduction)
        https://www.interactivebrokers.com/docs/tws-api/ref/contract.md
        /docs/tws-api/protobuf/introduction
        https://example.com/not-in-scope
        """
        self.assertEqual(
            sync.extract_markdown_links(text_value),
            {
                "https://www.interactivebrokers.com/docs/tws-api/doc/introduction",
                "https://www.interactivebrokers.com/docs/tws-api/ref/contract",
                "https://www.interactivebrokers.com/docs/tws-api/protobuf/introduction",
            },
        )

    def test_extracts_html_links(self) -> None:
        html_value = """
        <html><body>
          <a href="/docs/tws-api/doc/quick-start/order-id">Order ID</a>
          <a href="https://ibkrcampus.com/docs/tws-api/ref/order.md">Order</a>
          <a href="/docs/web-api/introduction">Other API</a>
        </body></html>
        """
        self.assertEqual(
            sync.extract_html_links(
                html_value,
                base_url="https://www.interactivebrokers.com/docs/tws-api/doc/introduction",
            ),
            {
                "https://www.interactivebrokers.com/docs/tws-api/doc/quick-start/order-id",
                "https://www.interactivebrokers.com/docs/tws-api/ref/order",
            },
        )

    def test_local_path_preserves_upstream_tree(self) -> None:
        self.assertEqual(
            sync.local_relative_path(
                "https://www.interactivebrokers.com/docs/tws-api/doc/market-data/historical-data"
            ).as_posix(),
            "docs/tws-api/doc/market-data/historical-data.md",
        )


class SitemapTests(unittest.TestCase):
    def test_urlset_filters_to_tws_api(self) -> None:
        xml_value = """<?xml version="1.0" encoding="UTF-8"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>https://www.interactivebrokers.com/docs/tws-api/doc/introduction</loc></url>
          <url><loc>https://www.interactivebrokers.com/docs/web-api/introduction</loc></url>
        </urlset>
        """
        pages, children = sync.parse_sitemap(xml_value)
        self.assertEqual(
            pages,
            {"https://www.interactivebrokers.com/docs/tws-api/doc/introduction"},
        )
        self.assertEqual(children, set())

    def test_sitemap_index_returns_children(self) -> None:
        xml_value = """<?xml version="1.0" encoding="UTF-8"?>
        <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <sitemap><loc>https://www.interactivebrokers.com/sitemap-docs.xml</loc></sitemap>
        </sitemapindex>
        """
        pages, children = sync.parse_sitemap(xml_value)
        self.assertEqual(pages, set())
        self.assertEqual(
            children,
            {"https://www.interactivebrokers.com/sitemap-docs.xml"},
        )


class HtmlConversionTests(unittest.TestCase):
    def test_article_conversion_excludes_navigation(self) -> None:
        html_value = """
        <html><body>
          <nav>Navigation noise</nav>
          <main>
            <article>
              <h1>Place Order</h1>
              <p>Submit an <code>Order</code> object.</p>
              <pre><code>client.placeOrder(order_id, contract, order)</code></pre>
            </article>
          </main>
        </body></html>
        """
        rendered = sync.render_html_document(
            html_value,
            source_url="https://www.interactivebrokers.com/docs/tws-api/doc/orders/place-order",
        )
        self.assertIn("# Place Order", rendered)
        self.assertIn("Order", rendered)
        self.assertIn("client.placeOrder", rendered)
        self.assertNotIn("Navigation noise", rendered)


class ManifestTests(unittest.TestCase):
    def test_manifest_is_deterministic(self) -> None:
        documents = [
            {
                "path": "docs/tws-api/ref/z.md",
                "url": "u2",
                "official_markdown_url": "m2",
                "source_url": "s2",
                "source_format": "html_to_markdown",
                "source_sha256": "2",
                "sha256": "b",
                "bytes": 2,
            },
            {
                "path": "docs/tws-api/doc/a.md",
                "url": "u1",
                "official_markdown_url": "m1",
                "source_url": "s1",
                "source_format": "html_to_markdown",
                "source_sha256": "1",
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
