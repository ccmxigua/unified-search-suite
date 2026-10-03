"""Contract tests for filtered search, body extraction and MCP options."""

import contextlib
import io
import json
import subprocess
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

import test_regressions as base

SEARCH = base.SEARCH
MCP = base.MCP
CONTENT = base.load_module("content_under_test", base.ROOT / "scripts/local-content-extract.py")


class FilterTests(unittest.TestCase):
    def setUp(self):
        SEARCH.set_request_deadline(None)

    def test_validate_domains_and_dates(self):
        normalized = SEARCH.build_filters("Example.COM., docs.python.org", "blog.example.com", "2025-01-01", "2025-01-01")
        self.assertEqual(normalized["include_domains"], ["example.com", "docs.python.org"])
        for args in [("https://example.com",), ("example.com/path",), ("*.example.com",), ("bad..com",),
                     (None, None, "2025-02-30"), (None, None, "2025-02-01", "2025-01-01")]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                SEARCH.build_filters(*args)

    def test_domain_boundaries_and_exclusion_precedence(self):
        filters = SEARCH.build_filters("example.com", "blog.example.com")
        results = [{"url": "https://" + host} for host in
                   ("example.com", "docs.example.com", "blog.example.com", "sub.blog.example.com", "example.com.evil.org", "notexample.com")]
        kept, stats = SEARCH._filter_results(results, filters)
        self.assertEqual([r["url"] for r in kept], ["https://example.com", "https://docs.example.com"])
        self.assertEqual(stats["removed"], 4)

    def test_dates_are_inclusive_utc_and_unknown_is_excluded(self):
        filters = SEARCH.build_filters(start_date="2025-01-02", end_date="2025-01-02")
        dates = ["2025-01-02T00:00:00Z", "2025-01-02T23:59:59Z", "Thu, 02 Jan 2025 12:00:00 GMT",
                 "2025-01-03T01:00:00+02:00", "2025-01-02T01:00:00+02:00", "2025-01-03", "", None, "unknown"]
        kept, stats = SEARCH._filter_results([{"url": "https://example.com", "published_date": d} for d in dates], filters)
        self.assertEqual([r["published_date"] for r in kept], dates[:4])
        self.assertEqual(stats, {"removed": 5, "unknown_date": 3})

    def test_native_provider_payloads_include_filters(self):
        filters = SEARCH.build_filters("example.com", "blog.example.com", "2025-01-02", "2025-01-03")
        response = MagicMock()
        response.json.return_value = {"results": []}
        with patch.object(SEARCH, "_post_exa_search", return_value=response) as post:
            SEARCH.search_exa("test", "test", filters=filters)
            payload = post.call_args.args[2]
            self.assertEqual(payload["includeDomains"], ["example.com"])
            self.assertEqual(payload["excludeDomains"], ["blog.example.com"])
            self.assertEqual(payload["startPublishedDate"], "2025-01-01T00:00:00.000Z")
            self.assertEqual(payload["endPublishedDate"], "2025-01-04T00:00:00.000Z")
        with patch.object(SEARCH.requests, "post", return_value=response) as post:
            SEARCH.search_tavily("test", "test", filters=filters)
            payload = post.call_args.kwargs["json"]
            self.assertEqual(payload["include_domains"], ["example.com"])
            self.assertEqual(payload["exclude_domains"], ["blog.example.com"])
            self.assertEqual(payload["start_date"], "2025-01-01")
            self.assertEqual(payload["end_date"], "2025-01-04")
            self.assertTrue(payload["filter_by_published_date"])

    def test_filters_reach_fast_deep_and_answer_providers(self):
        filters = SEARCH.build_filters("example.com")
        keys = {"exa": "test", "tavily": "test", "grok_key": "test", "grok_url": "https://example.com", "tinyfish": "test"}
        for mode in ("fast", "deep", "answer"):
            with self.subTest(mode=mode), patch.object(SEARCH, "search_exa", return_value=[]) as exa, \
                    patch.object(SEARCH, "search_tavily", return_value={"results": []}) as tavily, \
                    patch.object(SEARCH, "search_grok") as grok, patch.object(SEARCH, "search_tinyfish") as tinyfish:
                SEARCH.execute_search("test", mode, keys, 3, filters=filters)
                for provider in (exa, tavily):
                    if provider.called:
                        self.assertEqual(provider.call_args.kwargs["filters"], filters)
                grok.assert_not_called()
                tinyfish.assert_not_called()

    def test_filtered_main_omits_out_of_scope_results_and_synthesis(self):
        def execute(*args, **kwargs):
            self.assertEqual(kwargs["filters"]["include_domains"], ["example.com"])
            SEARCH._record_source_outcome("exa", "success")
            return [{"url": "https://example.com/a", "source": "exa"},
                    {"url": "https://other.com/a", "source": "exa"}], "Unverified answer"
        with patch.object(SEARCH, "_run_exa_research_light") as research:
            code, data = base.SearchTests().invoke(
                ["test comparison", "--include-domains", "example.com", "--intent", "comparison"], execute,
                keys={"exa": "test", "grok_key": "test", "grok_url": "https://example.com"})
            research.assert_not_called()
        self.assertEqual((code, data["count"]), (0, 1))
        self.assertNotIn("answer", data)
        self.assertEqual(data["provider_status"]["grok"], "unsupported_filters")
        self.assertEqual(data["filter_status"]["removed"], 1)

    def test_unsupported_filter_provider_is_explicit_error(self):
        code, data = base.SearchTests().invoke(["test", "--include-domains", "example.com"], None,
                                              keys={"grok_key": "test", "grok_url": "https://example.com"})
        self.assertEqual(code, 2)
        self.assertEqual(data["error"]["code"], "unsupported_search_filters")

    def test_bad_filter_combinations_fail_before_any_search(self):
        for flags in (["--start-date", "2025-01-02", "--freshness", "pw"],
                      ["--include-domains", "example.com", "--extract-refs-urls", "https://other.com"],
                      ["--read-top", "11"], ["--content-timeout", "nan"]):
            with self.subTest(flags=flags), patch.object(sys, "argv", ["search.py", "test", *flags]), \
                    patch.object(SEARCH, "get_keys") as keys, contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    SEARCH.main()
                self.assertEqual(error.exception.code, 2)
                keys.assert_not_called()


class ContentTests(unittest.TestCase):
    def test_unbounded_character_limits_are_rejected_before_fetch(self):
        for limit in (0, -1, 200001):
            with self.subTest(limit=limit), patch.object(CONTENT, "_fetch_html") as fetch:
                with self.assertRaises(ValueError):
                    CONTENT.extract_content("https://example.com", max_chars=limit)
                fetch.assert_not_called()

    def test_success_is_capped_and_does_not_use_external_fallback(self):
        with patch.object(CONTENT, "_fetch_html", return_value=("html", {"final_url": "https://example.com"})), \
                patch.object(CONTENT.trafilatura, "extract", return_value="Useful body. " * 50), \
                patch.object(CONTENT, "_mineru_extract") as mineru:
            result = CONTENT.extract_content("https://example.com", max_chars=100, fallback="mineru")
        self.assertEqual((result["ok"], result["quality"], len(result["markdown"])), (True, "sufficient", 100))
        self.assertTrue(result["truncated"])
        mineru.assert_not_called()

    def test_failed_local_extraction_uses_opted_in_mineru(self):
        with patch.object(CONTENT, "_fetch_html", side_effect=RuntimeError("unavailable")), \
                patch.object(CONTENT, "_mineru_extract", return_value=("Extracted content " * 30, {"cached": True})) as mineru:
            result = CONTENT.extract_content("https://example.com/paper.pdf", fallback="mineru")
        self.assertEqual(result["engine"], "mineru")
        self.assertTrue(result["ok"])
        self.assertEqual([a["engine"] for a in result["attempts"]], ["local_fetch", "mineru"])
        self.assertLessEqual(mineru.call_args.args[1], 30)

    def test_failed_fallback_preserves_short_local_body(self):
        with patch.object(CONTENT, "_fetch_html", return_value=("<p>Short</p>", {"final_url": "https://example.com"})), \
                patch.object(CONTENT.trafilatura, "extract", return_value="Short content"), \
                patch.object(CONTENT, "_mineru_extract", side_effect=ValueError("Missing token")):
            result = CONTENT.extract_content("https://example.com", fallback="mineru")
        self.assertEqual((result["ok"], result["status"], result["markdown"]), (True, "partial", "Short content"))
        self.assertIn("Missing token", result["notes"])

    def test_default_fallback_never_calls_mineru(self):
        with patch.object(CONTENT, "_fetch_html", side_effect=RuntimeError("unavailable")), \
                patch.object(CONTENT, "_mineru_extract") as mineru:
            result = CONTENT.extract_content("https://example.com")
        self.assertFalse(result["ok"])
        mineru.assert_not_called()

    def test_unsafe_urls_fail_before_any_fetch(self):
        for url in ("file:///etc/passwd", "http://127.0.0.1", "http://[::1]", "http://localhost", "https://u:p@example.com"):
            with self.subTest(url=url), patch.object(CONTENT, "_fetch_html") as fetch, patch.object(CONTENT, "_mineru_extract") as mineru:
                self.assertFalse(CONTENT.extract_content(url, fallback="mineru")["ok"])
                fetch.assert_not_called()
                mineru.assert_not_called()

    def test_redirect_to_private_address_is_not_requested(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status_code = 302
        response.headers = {"Location": "http://127.0.0.1"}
        with patch.object(CONTENT.requests, "get", return_value=response) as get:
            with self.assertRaises(ValueError):
                CONTENT._fetch_html("https://example.com", time.monotonic() + 10)
        get.assert_called_once()

    def test_unsafe_redirect_does_not_trigger_external_fallback(self):
        with patch.object(CONTENT, "_fetch_html", side_effect=CONTENT.UnsafeURL("Blocked redirect")), \
                patch.object(CONTENT, "_mineru_extract") as mineru:
            result = CONTENT.extract_content("https://example.com", fallback="mineru")
        self.assertEqual(result["status"], "error")
        mineru.assert_not_called()

    def test_http_body_size_is_bounded(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status_code = 200
        response.headers = {"Content-Type": "text/html"}
        response.iter_content.return_value = [b"x" * 11]
        with patch.object(CONTENT.requests, "get", return_value=response), patch.object(CONTENT, "MAX_BODY_BYTES", 10):
            with self.assertRaisesRegex(ValueError, "5 MiB"):
                CONTENT._fetch_html("https://example.com", time.monotonic() + 10)

    def test_search_content_timeout_preserves_search_rows(self):
        SEARCH.set_request_deadline(time.monotonic() + 5)
        results = [{"url": "https://example.com/a"}, {"url": "https://example.com/b"}]
        with patch.object(SEARCH.subprocess, "run", side_effect=subprocess.TimeoutExpired("extract", 1)):
            stats = SEARCH._read_result_content(results, 1, 1, 100, "none")
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(results[0]["content"]["error"], "content_timeout")
        self.assertNotIn("content", results[1])


class MCPFeatureTests(unittest.TestCase):
    def test_search_options_and_literal_query_reach_cli(self):
        result = subprocess.CompletedProcess([], 0, '{"status":"empty"}', "")
        with patch.object(MCP.subprocess, "run", return_value=result) as run:
            MCP.unified_search("--help", mode="fast", source="exa", num=3, include_domains=["python.org"],
                               start_date="2025-01-01", read_top=2, timeout=8)
        args = run.call_args.args[0]
        self.assertIn("--include-domains=python.org", args)
        self.assertIn("--read-top=2", args)
        self.assertEqual(args[-2:], ["--", "--help"])
        self.assertLessEqual(float(run.call_args.kwargs["env"]["UNIFIED_SEARCH_TIMEOUT_SECONDS"]), 8)

    def test_invalid_limits_do_not_launch_process(self):
        for call in (lambda: MCP.unified_search("test", num=0), lambda: MCP.unified_search("test", source="unknown"),
                     lambda: MCP.extract_content("https://example.com", max_chars=0),
                     lambda: MCP.fetch_thread("https://example.com", max_comments=0)):
            with patch.object(MCP.subprocess, "run") as run:
                self.assertEqual(json.loads(call())["error"]["code"], "invalid_arguments")
                run.assert_not_called()

    def test_native_thread_failure_is_not_reported_as_success(self):
        result = subprocess.CompletedProcess([], 0, '{"error":"fetch failed","comments":[]}', "")
        with patch.object(MCP.subprocess, "run", return_value=result):
            data = json.loads(MCP.fetch_thread("https://example.com"))
        self.assertEqual(data["status"], "error")


if __name__ == "__main__":
    unittest.main()
