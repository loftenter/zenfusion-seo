"""Offline tests for goto_resolver (no network)."""
import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "zenfusion_seo"))
import goto_resolver as g  # noqa: E402

G1 = "https://google.com/goto?url=CAESabc"
G2 = "https://www.google.com/goto?url=CAESdef"
DATA = {"tasks": [{"result": [{"items": [
    {"type": "organic", "url": G1, "domain": "google.com"},
    {"type": "organic", "url": "https://ok.com/", "domain": "ok.com"},
    {"type": "ai_overview", "references": [{"url": G2, "domain": "www.google.com"}], "links": [G1]},
]}]}]}
MAP = {G1: "https://a.com/x", G2: "https://b.com/y"}


class GotoResolverTests(unittest.TestCase):
    def test_collect(self):
        self.assertEqual(sorted(g.collect_goto_urls(DATA)), sorted([G1, G2]))

    def test_parse_shapes(self):
        shapes = [
            {"tasks": [{"result": [{"items": [{"goto_url": G1, "url": MAP[G1]}, {"goto_url": G2, "url": MAP[G2]}]}]}]},
            {"tasks": [{"result": [{G1: MAP[G1], G2: MAP[G2]}]}]},
            {"tasks": [{"result": [{"urls": [MAP[G1], MAP[G2]]}]}]},
        ]
        for resp in shapes:
            self.assertEqual(g._parse_converter(resp, [G1, G2]), MAP)

    def test_apply_fixes_url_and_domain(self):
        d = copy.deepcopy(DATA)
        g.apply_mapping(d, MAP)
        items = d["tasks"][0]["result"][0]["items"]
        self.assertEqual(items[0]["url"], MAP[G1])
        self.assertEqual(items[0]["domain"], "a.com")
        self.assertEqual(items[0]["goto_url"], G1)
        self.assertEqual(items[1]["domain"], "ok.com")
        self.assertEqual(items[2]["references"][0]["domain"], "b.com")
        self.assertEqual(items[2]["links"], [MAP[G1]])

    def test_non_serp_endpoint_untouched(self):
        self.assertEqual(g.fix_goto_urls({"x": G1}, "backlinks/summary/live"), {"x": G1})

    def test_disabled_by_env(self):
        os.environ["ZENFUSION_GOTO_RESOLVE"] = "0"
        try:
            self.assertEqual(g.fix_goto_urls({"x": G1}, "serp/google/organic/live/advanced"), {"x": G1})
        finally:
            del os.environ["ZENFUSION_GOTO_RESOLVE"]


if __name__ == "__main__":
    unittest.main()
