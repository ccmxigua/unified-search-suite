"""MinerU task recovery and real local PDF text extraction regressions."""

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
import zipfile
from unittest.mock import patch

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

import test_regressions as base
import test_search_features as features

MINERU = base.MINERU
CONTENT = features.CONTENT


def pdf_bytes(texts):
    writer = PdfWriter()
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    for text in texts:
        page = writer.add_blank_page(width=600, height=800)
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 50 700 Td ({text}) Tj ET".encode("ascii"))
        page[NameObject("/Contents")] = stream
    target = io.BytesIO()
    writer.write(target)
    return target.getvalue()


class PDFExtractionTests(unittest.TestCase):
    def extract(self, body, **kwargs):
        with patch.object(CONTENT, "_fetch_html", return_value=(body, {"final_url": "https://example.com/file.pdf"})):
            return CONTENT.extract_content("https://example.com/file.pdf", **kwargs)

    def test_complete_short_text_pdf_avoids_cloud_queue(self):
        with patch.object(CONTENT, "_mineru_extract") as mineru:
            result = self.extract(pdf_bytes(["Short document."]), fallback="mineru")
        self.assertEqual((result["status"], result["engine"], result["markdown"]), ("success", "pypdf", "Short document."))
        self.assertEqual(result["artifacts"]["pdf_pages_read"], 1)
        mineru.assert_not_called()

    def test_blank_pdf_is_not_misreported_as_extracted(self):
        result = self.extract(pdf_bytes([""]))
        self.assertEqual(result["status"], "error")
        self.assertIsNone(result["markdown"])
        self.assertTrue(result["artifacts"]["pdf_text_incomplete"])

    def test_mixed_pdf_preserves_text_and_resume_handle_on_cloud_failure(self):
        error = MINERU.MinerUTaskError("Service still pending", task_id="job-one", state="pending", checkpoint_path="/tmp/test-task.json")
        with patch.object(CONTENT, "_mineru_extract", side_effect=error):
            result = self.extract(pdf_bytes(["Useful text. " * 30, ""]), fallback="mineru")
        self.assertEqual(result["status"], "partial")
        self.assertIn("Useful text.", result["markdown"])
        self.assertEqual(result["mineru_task"]["task_id"], "job-one")
        self.assertTrue(result["mineru_task"]["resumable"])

    def test_page_and_character_limits(self):
        result = self.extract(pdf_bytes(["a" * 600, "Second page"]), max_chars=100)
        self.assertEqual(len(result["markdown"]), 100)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["artifacts"]["pdf_pages_read"], 1)
        result = self.extract(pdf_bytes([""] * 201))
        self.assertEqual(result["status"], "error")
        self.assertIn("200-page", result["notes"][0])

    def test_pdf_without_extension_passes_detected_type_to_cloud(self):
        url = "https://example.com/download?id=1"
        with patch.object(CONTENT, "_fetch_html", return_value=(pdf_bytes([""]), {"final_url": url})), \
                patch.object(CONTENT, "_mineru_extract", return_value=("Cloud text " * 30, {})) as cloud:
            result = CONTENT.extract_content(url, fallback="mineru")
        self.assertTrue(cloud.call_args.kwargs["is_pdf"])
        self.assertEqual(result["engine"], "mineru")

    def test_pdf_worker_is_killed_at_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = Path(directory) / "pdf-text-extract.py"
            worker.write_text("import time\ntime.sleep(10)\n")
            start = time.monotonic()
            with patch.object(CONTENT, "__file__", str(Path(directory) / "local-content-extract.py")):
                with self.assertRaisesRegex(TimeoutError, "deadline"):
                    CONTENT._extract_pdf(b"%PDF-", start + 0.2, 100)
            self.assertLess(time.monotonic() - start, 3)

    def test_compressed_oversized_pdf_stream_is_rejected(self):
        from pypdf import PdfReader
        writer = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf_bytes(["a" * (17 * 1024 * 1024)]))))
        writer.pages[0].compress_content_streams()
        target = io.BytesIO()
        writer.write(target)
        self.assertLess(len(target.getvalue()), 5 * 1024 * 1024)
        result = self.extract(target.getvalue())
        self.assertEqual(result["status"], "error")
        self.assertTrue(result["notes"])


class MinerURecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {"OPENCLAW_WORKSPACE": self.temp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.kwargs = dict(api_base="https://example.com", token="test", source_url="https://example.com/input.pdf",
                           enable_ocr=False, language="ch", page_ranges=None, model_version="pipeline",
                           enable_table=None, enable_formula=None, extra_formats=None,
                           timeout_sec=10, poll_interval=1, cache=True, force=False)
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as target:
            target.writestr("full.md", "Recovered document body.")
        self.archive = archive.getvalue()
        self.done = {"state": "done", "full_zip_url": "https://example.com/result.zip"}

    def pending(self, **kwargs):
        kwargs["on_update"]({"state": "pending"})
        raise TimeoutError("deadline exhausted")

    def invoke(self, **changes):
        with contextlib.redirect_stderr(io.StringIO()):
            return MINERU.parse_one_url(**{**self.kwargs, **changes})

    def test_timeout_retry_resumes_and_then_reads_validated_cache(self):
        with patch.object(MINERU, "create_task", return_value="job-one") as create, \
                patch.object(MINERU, "poll_task", side_effect=self.pending):
            with self.assertRaises(MINERU.MinerUTaskError) as failure:
                self.invoke()
        saved = Path(failure.exception.task["checkpoint_path"])
        self.assertEqual(json.loads(saved.read_text())["state"], "pending")
        self.assertNotIn("token", saved.read_text())
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        with patch.object(MINERU, "create_task") as create_again, \
                patch.object(MINERU, "poll_task", return_value=self.done) as poll, \
                patch.object(MINERU, "_http_bytes", return_value=self.archive):
            recovered = self.invoke()
            cached = self.invoke()
        create_again.assert_not_called()
        self.assertEqual(poll.call_args.kwargs["task_id"], "job-one")
        self.assertTrue(recovered["resumed"])
        self.assertTrue(cached["cached"])
        self.assertEqual(Path(recovered["markdown_path"]).read_text(), "Recovered document body.")

    def test_download_failure_and_corrupt_archive_do_not_submit_again(self):
        with patch.object(MINERU, "create_task", return_value="job-one") as create, \
                patch.object(MINERU, "poll_task", return_value=self.done), \
                patch.object(MINERU, "_http_bytes", side_effect=[TimeoutError("download timeout"), b"invalid zip", self.archive]):
            for _ in range(2):
                with self.assertRaises(MINERU.MinerUTaskError):
                    self.invoke()
            recovered = self.invoke()
        self.assertEqual(create.call_count, 1)
        self.assertTrue(recovered["resumed"])

    def test_failed_task_requires_explicit_force(self):
        def fail(**kwargs):
            kwargs["on_update"]({"state": "failed", "err_msg": "Model unavailable"})
            raise RuntimeError("Model unavailable")
        with patch.object(MINERU, "create_task", return_value="job-one"), patch.object(MINERU, "poll_task", side_effect=fail):
            with self.assertRaises(MINERU.MinerUTaskError) as failure:
                self.invoke()
        self.assertFalse(failure.exception.task["resumable"])
        with patch.object(MINERU, "create_task") as create, patch.object(MINERU, "poll_task") as poll:
            with self.assertRaises(MINERU.MinerUTaskError):
                self.invoke()
            create.assert_not_called()
            poll.assert_not_called()
        with patch.object(MINERU, "create_task", return_value="job-two") as create, \
                patch.object(MINERU, "poll_task", return_value=self.done), patch.object(MINERU, "_http_bytes", return_value=self.archive):
            self.assertEqual(self.invoke(force=True)["task_id"], "job-two")
            create.assert_called_once()

    def test_backend_and_request_parameters_isolate_tasks(self):
        with patch.object(MINERU, "create_task", side_effect=["one", "two", "three"]) as create, \
                patch.object(MINERU, "poll_task", side_effect=self.pending):
            for change in ({}, {"api_base": "https://other.example"}, {"page_ranges": "1"}):
                with self.assertRaises(MINERU.MinerUTaskError):
                    self.invoke(**change)
        self.assertEqual(create.call_count, 3)

    def test_corrupt_checkpoint_fails_closed_without_duplicate_submission(self):
        with patch.object(MINERU, "create_task", return_value="job-one"), patch.object(MINERU, "poll_task", side_effect=self.pending):
            with self.assertRaises(MINERU.MinerUTaskError) as failure:
                self.invoke()
        Path(failure.exception.task["checkpoint_path"]).write_text("{")
        with patch.object(MINERU, "create_task") as create:
            with self.assertRaisesRegex(ValueError, "checkpoint"):
                self.invoke()
            create.assert_not_called()

    def test_no_cache_still_resumes_pending_task(self):
        with patch.object(MINERU, "create_task", return_value="job-one") as create, \
                patch.object(MINERU, "poll_task", side_effect=self.pending):
            for _ in range(2):
                with self.assertRaises(MINERU.MinerUTaskError):
                    self.invoke(cache=False)
        self.assertEqual(create.call_count, 1)

    def test_official_endpoint_preserves_legacy_completed_cache(self):
        payload = dict(url=self.kwargs["source_url"], model_version="pipeline", language="ch", is_ocr=False)
        key = MINERU._cache_key(payload)
        directory = Path(self.temp.name) / "mineru-cache" / key
        directory.mkdir(parents=True)
        (directory / "full.md").write_text("Existing completed result")
        (directory / "result.zip").write_bytes(self.archive)
        (directory / "meta.json").write_text(json.dumps({
            "ok": True, "source": payload["url"], "model_version": "pipeline", "cache_key": key,
            "markdown_path": str(directory / "full.md"), "zip_path": str(directory / "result.zip")}))
        with patch.object(MINERU, "create_task") as create:
            result = self.invoke(api_base="https://mineru.net")
        self.assertTrue(result["cached"])
        create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
