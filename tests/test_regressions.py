"""Offline behavioral regressions; no credentials or network calls required."""

import asyncio
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "vendor/openclaw-search-skills"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SEARCH = load_module("search_under_test", VENDOR / "search-layer/scripts/search.py")
THREAD = load_module("thread_under_test", VENDOR / "search-layer/scripts/fetch_thread.py")
MINERU = load_module("mineru_under_test", VENDOR / "mineru-extract/scripts/mineru_parse_documents.py")
MCP = load_module("mcp_under_test", ROOT / "mcp-server.py")


class WrapperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.scripts = self.root / "scripts"
        self.scripts.mkdir()
        shutil.copy2(ROOT / "scripts/unified-search.sh", self.scripts)
        recorder = self.root / "record.py"
        recorder.write_text("import json, sys\nprint(json.dumps({'route': sys.argv[1], 'args': sys.argv[2:]}))\n")
        for route in ("search-layer", "fetch-thread", "content-extract", "mineru-extract", "mineru-parse-documents"):
            (self.scripts / f"run-{route}.sh").write_text(
                f"exec {shlex.quote(sys.executable)} {shlex.quote(str(recorder))} {route} \"$@\"\n"
            )
        self.env = dict(os.environ, UNIFIED_SEARCH_DISABLE_TRANSLATION="1", UNIFIED_SEARCH_SAVE_DIR="")

    def run_wrapper(self, *args):
        return subprocess.run(
            ["bash", str(self.scripts / "unified-search.sh"), *args],
            text=True, capture_output=True, env=self.env, cwd=self.root, timeout=10,
        )

    def assert_route(self, expected_route, expected_args, *args):
        result = self.run_wrapper(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"route": expected_route, "args": expected_args})

    def test_explicit_subcommands_preserve_native_options(self):
        cases = [
            ("fetch-thread", ["https://github.com/a/b/issues/1", "--format", "markdown"]),
            ("content-extract", ["--url", "https://example.com", "--max-chars", "100"]),
            ("mineru-extract", ["https://example.com/file.pdf", "--model", "pipeline"]),
            ("mineru-parse-documents", ["--file-sources", "https://example.com/file.pdf", "--no-cache"]),
            ("search-layer", ["--help"]),
        ]
        for route, args in cases:
            with self.subTest(route=route):
                self.assert_route(route, args, route, *args)

    def test_automatic_urls_preserve_options(self):
        cases = [
            ("fetch-thread", "https://news.ycombinator.com/item?id=1", ["--format", "markdown", "--timeout", "9"]),
            ("mineru-extract", "https://example.com/file.pdf", ["--model", "pipeline", "--timeout", "9"]),
            ("content-extract", "https://example.com/page", ["--max-chars", "100", "--timeout", "9"]),
        ]
        for route, url, options in cases:
            with self.subTest(route=route):
                expected = (["--url"] if route == "content-extract" else []) + [url, *options]
                self.assert_route(route, expected, url, *options)

    def test_plain_query_and_search_flags(self):
        self.assert_route("search-layer", ["Python", "--source", "exa", "--num", "3"],
                          "Python", "--source", "exa", "--num", "3")

    def test_literal_query_is_not_consumed_as_wrapper_options(self):
        args = ["--", "--save-run", "somewhere", "--fast"]
        self.assert_route("search-layer", args, "search-layer", *args)

    def test_literal_default_query_keeps_search_features(self):
        for query in ("--help", "--fast", "--save-run", "fetch-thread", "https://example.com", "中文查询"):
            with self.subTest(query=query):
                self.assert_route("search-layer", ["--intent", "factual", "--mode", "deep", "--source",
                                                   "exa,tavily,grok,tinyfish", "--", query], "--", query)

    def test_explicit_mode_equals_overrides_alias(self):
        self.assert_route("search-layer", ["query", "--mode=answer"], "query", "--mode=answer", "--fast")

    def test_refs_urls_are_search_arguments(self):
        args = ["--extract-refs-urls", "https://example.com/a", "https://example.com/b"]
        self.assert_route("search-layer", args, *args)

    def test_save_run_preserves_native_args_and_stdout(self):
        save_dir = self.root / "saved results"
        result = self.run_wrapper("fetch-thread", "https://example.com", "--format", "json", "--save-run", str(save_dir))
        self.assertEqual(result.returncode, 0, result.stderr)
        files = list(save_dir.glob("*.json"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_text(), result.stdout)

    def test_unknown_search_option_fails(self):
        result = self.run_wrapper("query", "--bogus", "value")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Unknown option", result.stderr)

    def test_save_run_missing_value_fails(self):
        self.assertEqual(self.run_wrapper("query", "--save-run").returncode, 2)

    def test_save_run_does_not_consume_option_or_literal_boundary(self):
        for value in ("--", "--num"):
            with self.subTest(value=value):
                result = self.run_wrapper("query", "--save-run", value, "literal-query")
                self.assertEqual(result.returncode, 2)
                self.assertFalse((self.root / value).exists())

    def test_missing_legacy_route_fails(self):
        self.assertEqual(self.run_wrapper("legacy", "query").returncode, 127)


class SearchTests(unittest.TestCase):
    def invoke(self, args, execute, keys=None, refs=None):
        output = io.StringIO()
        with patch.object(sys, "argv", ["search.py", *args]), \
                patch.object(SEARCH, "get_keys", return_value=keys if keys is not None else {"tavily": "test"}), \
                patch.object(SEARCH, "execute_search", side_effect=execute), \
                patch.object(SEARCH, "_populate_chinese_summaries"), \
                patch.object(SEARCH, "_translate_to_zh", return_value=""), \
                patch.object(SEARCH, "_run_extract_refs", return_value=refs or []), \
                contextlib.redirect_stdout(output):
            try:
                code = SEARCH.main()
            except SystemExit as exc:
                code = exc.code
        return code, json.loads(output.getvalue())

    def test_multi_query_scores_do_not_depend_on_query_order(self):
        def execute(query, *args, **kwargs):
            SEARCH._record_source_outcome("tavily", "success")
            return [{"title": query, "snippet": query, "url": "https://example.com/" + query.split()[0], "source": "tavily"}], None
        queries = ["Rust async", "Python dataframe"]
        scores = []
        for order in (queries, list(reversed(queries))):
            code, data = self.invoke(["--queries", *order, "--intent", "comparison"], execute)
            self.assertEqual(code, 0)
            scores.append({r["title"]: r["score"] for r in data["results"]})
        self.assertEqual(scores[0], scores[1])
        self.assertEqual(scores[0][queries[0]], scores[0][queries[1]])

    def test_status_and_exit_code(self):
        cases = [("success", [], "empty", 0), ("error", [], "error", 1)]
        for state, results, expected_status, expected_code in cases:
            def execute(*args, **kwargs):
                SEARCH._record_source_outcome("tavily", state)
                return results, None
            with self.subTest(state=state):
                code, data = self.invoke(["query"], execute)
                self.assertEqual(data["status"], expected_status)
                self.assertEqual(code, expected_code)

    def test_partial_provider_failure_keeps_results(self):
        def execute(*args, **kwargs):
            SEARCH._record_source_outcome("tavily", "success")
            SEARCH._record_source_outcome("exa", "error")
            return [{"title": "query", "url": "https://example.com", "source": "tavily"}], None
        code, data = self.invoke(["query"], execute, {"tavily": "test", "exa": "test"})
        self.assertEqual((code, data["status"], data["count"]), (0, "partial", 1))
        self.assertEqual(data["provider_errors"], ["exa"])

    def test_all_reference_failures_exit_nonzero(self):
        code, data = self.invoke(["--extract-refs-urls", "https://example.com"], None,
                                 refs=[{"source_url": "https://example.com", "error": "unavailable"}])
        self.assertEqual((code, data["status"]), (1, "error"))

    def test_missing_provider_is_an_error(self):
        code, data = self.invoke(["query"], None, keys={})
        self.assertEqual(code, 2)
        self.assertEqual(data["error"]["code"], "no_search_provider")


class ThreadTests(unittest.TestCase):
    def test_hn_budget_counts_nested_comments(self):
        def comment(name, children=None):
            return {"author": name, "text": name, "children": children or []}
        item = {"title": "Thread", "num_comments": 5,
                "children": [comment("one", [comment("two"), comment("three")]), comment("four"), comment("five")]}
        with patch.object(THREAD, "_http_get", return_value={"status": 200, "json": item}), \
                patch.object(THREAD, "_find_github_token", return_value=None):
            result = THREAD.fetch_thread_url("https://news.ycombinator.com/item?id=1", max_comments=2)
        self.assertEqual(result["metadata"]["fetched_comment_count"], 2)
        self.assertEqual(len(list(THREAD._iter_nested_comments(result["comments_tree"]))), 2)
        self.assertIn("comments_limit_reached", result["truncation_reasons"])

    def test_hn_exact_budget_is_not_truncated(self):
        item = {"title": "Thread", "num_comments": 2,
                "children": [{"author": "one", "text": "One"}, {"author": "two", "text": "Two"}]}
        with patch.object(THREAD, "_http_get", return_value={"status": 200, "json": item}):
            result = THREAD.fetch_hn("https://news.ycombinator.com/item?id=1", max_comments=2)
        self.assertEqual(result["metadata"]["fetched_comment_count"], 2)
        self.assertFalse(result.get("truncated", False))

    def test_v2ex_respects_requested_budget(self):
        responses = [{"status": 200, "json": [{"title": "Thread", "replies": 3}]},
                     {"status": 200, "json": [{"content": str(i)} for i in range(3)]}]
        with patch.object(THREAD, "_http_get", side_effect=responses), \
                patch.object(THREAD, "_find_github_token", return_value=None):
            result = THREAD.fetch_thread_url("https://www.v2ex.com/t/1", max_comments=2)
        self.assertEqual(len(result["comments"]), 2)
        self.assertEqual(result["metadata"]["fetched_comment_count"], 2)
        self.assertIn("comments_limit_reached", result["truncation_reasons"])


class MCPTests(unittest.TestCase):
    def test_query_is_always_literal_search_text(self):
        for query in ("fetch-thread", "--help", "--save-run /tmp/example", "https://example.com"):
            result = subprocess.CompletedProcess([], 0, '{"status":"empty","results":[]}', "")
            with self.subTest(query=query), patch.object(MCP.subprocess, "run", return_value=result) as run:
                MCP.unified_search(query)
                self.assertEqual(run.call_args.args[0], ["bash", MCP.UNIFIED_SEARCH_BIN, "--", query])

    def test_diagnostics_preserve_structured_status(self):
        for status in ("partial", "empty", "error", "timeout", "success"):
            payload = {"status": status, "results": []}
            result = subprocess.CompletedProcess([], 0, json.dumps(payload), "[diagnostic] message\n")
            with self.subTest(status=status), patch.object(MCP.subprocess, "run", return_value=result):
                data = json.loads(MCP.unified_search("query"))
                self.assertEqual(data["status"], status)
                self.assertEqual(data["result"], payload)

    def test_timeout_captures_partial_output(self):
        error = subprocess.TimeoutExpired("search", 1, output=b"partial", stderr=b"diagnostic")
        with patch.object(MCP.subprocess, "run", side_effect=error):
            data = json.loads(MCP.unified_search("query"))
        self.assertEqual((data["status"], data["stdout"], data["stderr"]), ("timeout", "partial", "diagnostic"))


class RuntimeTests(unittest.TestCase):
    def test_setup_rejects_old_existing_venv_before_pip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            shutil.copy2(ROOT / "scripts/setup-venv.sh", root / "scripts")
            (root / ".venv/bin").mkdir(parents=True)
            fake_python = root / ".venv/bin/python"
            fake_python.write_text('#!/bin/bash\nif [[ "$1" == "-c" ]]; then exit 1; fi\necho PIP_WAS_RUN\n')
            fake_python.chmod(0o755)
            result = subprocess.run(["bash", str(root / "scripts/setup-venv.sh")],
                                    env=dict(os.environ, PYTHON_BIN=sys.executable),
                                    text=True, capture_output=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("PIP_WAS_RUN", result.stdout)
            self.assertIn("3.10", result.stderr)

    def test_mineru_workspace_is_resolved_after_dotenv(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "scripts/mineru_parse_documents.py"
            script.parent.mkdir()
            script.write_text("")
            workspace = root / "configured workspace"
            (root / ".env").write_text(f'OPENCLAW_WORKSPACE="{workspace}"\n')
            with patch.dict(os.environ, {}, clear=True), patch.object(MINERU, "__file__", str(script)):
                MINERU._bootstrap_env()
                archive = io.BytesIO()
                with zipfile.ZipFile(archive, "w") as z:
                    z.writestr("document.md", "# Extracted document\n\nVerified content.")
                with patch.object(MINERU, "create_task", return_value="test-task") as create, \
                        patch.object(MINERU, "poll_task", return_value={"state": "done", "full_zip_url": "https://example.com/result.zip"}), \
                        patch.object(MINERU, "_http_bytes", return_value=archive.getvalue()):
                    kwargs = dict(api_base="https://example.com", token="test", source_url="https://example.com/input.pdf",
                                  enable_ocr=False, language="ch", page_ranges=None, model_version=None,
                                  enable_table=None, enable_formula=None, extra_formats=None,
                                  timeout_sec=10, poll_interval=1, cache=True, force=False)
                    result = MINERU.parse_one_url(**kwargs)
                    self.assertEqual(Path(result["out_dir"]).parent, workspace / "mineru-cache")
                    self.assertTrue(Path(result["markdown_path"]).is_file())
                    cached = MINERU.parse_one_url(**kwargs)
                    self.assertTrue(cached["cached"])
                    create.assert_called_once()

    def test_mineru_process_env_takes_precedence_over_dotenv(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "scripts/mineru_parse_documents.py"
            script.parent.mkdir()
            (root / ".env").write_text("OPENCLAW_WORKSPACE=/unused\n")
            with patch.dict(os.environ, {"OPENCLAW_WORKSPACE": str(root)}, clear=True), \
                    patch.object(MINERU, "__file__", str(script)):
                MINERU._bootstrap_env()
                self.assertEqual(MINERU._cache_root(), root / "mineru-cache")


class MCPProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_initialize_list_and_call(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        async def exercise():
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                shutil.copy2(ROOT / "mcp-server.py", root)
                (root / "scripts").mkdir()
                recorder = root / "record.py"
                recorder.write_text("import json, sys\nprint(json.dumps({'status': 'success', 'args': sys.argv[1:]}))\n")
                (root / "scripts/unified-search.sh").write_text(
                    f"exec {shlex.quote(sys.executable)} {shlex.quote(str(recorder))} \"$@\"\n"
                )
                params = StdioServerParameters(command=sys.executable, args=[str(root / "mcp-server.py")])
                with (root / "server.stderr").open("w") as errlog:
                    async with stdio_client(params, errlog=errlog) as (read, write):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            listed = await session.list_tools()
                            self.assertEqual([tool.name for tool in listed.tools], ["unified_search"])
                            result = await session.call_tool("unified_search", {"query": "--save-run"})
                            self.assertFalse(result.isError)
                            self.assertEqual(json.loads(result.content[0].text),
                                             {"status": "success", "args": ["--", "--save-run"]})

        await asyncio.wait_for(exercise(), timeout=20)


if __name__ == "__main__":
    unittest.main()
