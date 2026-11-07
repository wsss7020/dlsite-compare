# -*- coding: utf-8 -*-
"""DLsite 比对工具后端回归测试：全部使用内存模拟数据，不联网、不写真实数据。"""
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import rescheck  # noqa: E402
import server  # noqa: E402


class VersionGroupTests(unittest.TestCase):
    def setUp(self):
        self.item = {
            "rj": "RJ01440054",
            "editions": [
                {"workno": "RJ01440054"},
                {"workno": "RJ01491733"},
            ],
        }
        self.tinfo = {
            "RJ01440054": {},
            "RJ01491733": {"c": ["RJ01491734"], "o": "RJ01440054"},
            "RJ01491734": {"p": "RJ01491733", "o": "RJ01440054"},
        }
        self.alias = {"RJ01491734": "RJ01491733"}

    def test_recursively_includes_translation_station_child(self):
        group = server.version_rjs_of(self.item, tinfo=self.tinfo, alias=self.alias)
        self.assertEqual(group, ["RJ01440054", "RJ01491733", "RJ01491734"])

    def test_cycles_terminate_without_duplicates(self):
        tinfo = {
            "RJ000001": {"o": "RJ000002", "c": ["RJ000002"]},
            "RJ000002": {"o": "RJ000001", "c": ["RJ000001"]},
        }
        group = server.version_rjs_of(
            {"rj": "RJ000001", "editions": [{"workno": "RJ000002"}]},
            tinfo=tinfo, alias={"RJ000002": "RJ000001"})
        self.assertEqual(group, ["RJ000001", "RJ000002"])

    def test_resource_aggregation_uses_supplied_shared_maps(self):
        cache = {
            "RJ01440054": {"asmrone": {"f": False, "u": "old", "v": 1}},
            "RJ01491734": {"asmrone": {"f": True, "u": "new", "v": 2}},
        }
        # 传入共享映射后不应再逐作品读取真实 JSON。
        with mock.patch.object(server, "load_tinfo", side_effect=AssertionError("重复读 tinfo")), \
             mock.patch.object(server, "load_alias", side_effect=AssertionError("重复读 alias")):
            out = server.resources_from_cache(
                self.item, cache, tinfo=self.tinfo, alias=self.alias)
        self.assertTrue(out["asmrone"]["f"])
        self.assertEqual(out["asmrone"]["via"], "RJ01491734")
        self.assertEqual(out["asmrone"]["u"], "new")


class ResourceCheckTests(unittest.TestCase):
    def test_rj_hit_requires_digit_boundary(self):
        self.assertTrue(rescheck.rj_hit("[RJ285088] title", "RJ285088"))
        self.assertFalse(rescheck.rj_hit("RJ2850887", "RJ285088"))
        self.assertFalse(rescheck.rj_hit("RJ285088", "RJ285089"))

    def test_asmrone_uses_real_search_endpoint(self):
        payload = json.dumps({"works": [{"source_id": "RJ01491734"}]})
        with mock.patch.object(rescheck, "make_opener", return_value=object()), \
             mock.patch.object(rescheck, "http_get", return_value=("https://x", payload)) as get:
            out = rescheck.check_asmrone("RJ01491734", "none")
        url = get.call_args.args[1]
        self.assertIn("/api/search/RJ01491734", url)
        self.assertIn("includeTranslationWorks=true", url)
        self.assertNotIn("/api/works?keyword=", url)
        self.assertTrue(out["f"])

    def test_asmrone_rejects_adjacent_rj(self):
        payload = json.dumps({"works": [{"source_id": "RJ014917340"}]})
        with mock.patch.object(rescheck, "make_opener", return_value=object()), \
             mock.patch.object(rescheck, "http_get", return_value=("https://x", payload)):
            out = rescheck.check_asmrone("RJ01491734", "none")
        self.assertFalse(out["f"])

    def test_asmrone_old_cache_version_is_invalid(self):
        self.assertFalse(rescheck.cache_valid("asmrone", {"f": False}))
        self.assertFalse(rescheck.cache_valid("asmrone", {"f": False, "v": 1}))
        self.assertTrue(rescheck.cache_valid("asmrone", {"f": False, "v": 2}))
        # 其他站点旧格式没有 v 字段，仍然有效。
        self.assertTrue(rescheck.cache_valid("alist", {"f": True}))

    def test_check_site_marks_asmrone_cache_version_even_on_error(self):
        with mock.patch.dict(rescheck.CHECKERS, {"asmrone": mock.Mock(side_effect=RuntimeError("x"))}):
            out = rescheck.check_site("asmrone", "RJ01491734", "none")
        self.assertIsNone(out["f"])
        self.assertEqual(out["v"], 2)


class FrontEndWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    def test_local_recheck_never_replaces_whole_item(self):
        self.assertNotIn("RESULT.items[i] = r.item;", self.html)
        self.assertIn("RESULT.items[i] = {...RESULT.items[i], ...r.item};", self.html)

    def test_local_recheck_synchronizes_global_lists(self):
        start = self.html.index("async function recheckItem(")
        end = self.html.index("\nfunction startRename", start)
        block = self.html[start:end]
        for field in ("r.ignored", "r.ignored_makers", "r.favorites"):
            self.assertIn(field, block)
        self.assertIn("recomputeIgnoredFlags();", block)

    def test_server_local_recheck_returns_global_lists(self):
        source = (ROOT / "server.py").read_text(encoding="utf-8")
        start = source.index('elif path == "/api/recheck":')
        end = source.index('elif path == "/api/rename":', start)
        block = source[start:end]
        for field in ('"ignored": rec.get', '"ignored_makers": rec.get', '"favorites": rec.get'):
            self.assertIn(field, block)


class PublicReleaseSafetyTests(unittest.TestCase):
    def test_only_dlsite_urls_are_accepted(self):
        base, _qs = server.normalize_search_url("https://www.dlsite.com/maniax/fsr/")
        self.assertTrue(base.startswith("https://www.dlsite.com/"))
        for url in (
            "https://example.com/maniax/fsr/",
            "https://www.dlsite.com.evil.example/maniax/fsr/",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                server.normalize_search_url(url)

    def test_cookie_is_not_sent_to_lookalike_host(self):
        class Opener:
            request = None

            def open(self, request, timeout=30):
                self.request = request
                raise RuntimeError("stop before network")

        for host, should_have_cookie in (
            ("www.dlsite.com", True),
            ("www.dlsite.com.evil.example", False),
        ):
            opener = Opener()
            with self.subTest(host=host), \
                 mock.patch.object(server, "get_config", return_value={"cookie": "secret=test"}), \
                 mock.patch.object(server, "make_opener", return_value=opener), \
                 self.assertRaisesRegex(RuntimeError, "stop before network"):
                server.fetch(f"https://{host}/maniax/", "none")
            cookie = opener.request.get_header("Cookie") or ""
            self.assertEqual("secret=test" in cookie, should_have_cookie)


if __name__ == "__main__":
    unittest.main(verbosity=2)
