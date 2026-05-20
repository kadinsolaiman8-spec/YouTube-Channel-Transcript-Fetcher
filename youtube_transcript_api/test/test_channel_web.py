"""Tests for the channel export web API."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from youtube_transcript_api.channel.models import ExportConfig, FilterConfig, ScrapeConfig
from youtube_transcript_api.channel.web import app as web_app
from youtube_transcript_api.channel.web.app import JobState, JobStatus


class TestChannelWebApp(TestCase):
    def setUp(self) -> None:
        with web_app._jobs._lock:
            web_app._jobs._jobs.clear()
            web_app._jobs._active_job_id = None

    def test_serve_index(self) -> None:
        client = TestClient(web_app.app)
        response = client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Channel Transcript Export", response.text)

    @patch("youtube_transcript_api.channel.web.app._start_export_worker")
    def test_create_job_starts_worker(self, mock_start) -> None:
        client = TestClient(web_app.app)
        response = client.post(
            "/api/jobs",
            json={
                "channel_url": "https://www.youtube.com/@example",
                "filter_config": {"max_videos": 5},
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("job_id", response.json())
        mock_start.assert_called_once()

    @patch("youtube_transcript_api.channel.web.app._start_export_worker")
    def test_create_job_rejects_invalid_channel_url(self, mock_start) -> None:
        client = TestClient(web_app.app)
        response = client.post(
            "/api/jobs",
            json={"channel_url": "https://www.youtube.com/@"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("handle", response.json()["detail"].lower())
        mock_start.assert_not_called()

    @patch("youtube_transcript_api.channel.web.app._start_export_worker")
    def test_create_job_cancels_active_and_starts_new(self, mock_start) -> None:
        mock_process = MagicMock()
        mock_process.is_alive.return_value = True
        busy_job = JobState(
            job_id="busy-job",
            channel_url="https://www.youtube.com/@busy",
            filter_config=FilterConfig(),
            export_config=ExportConfig(),
            scrape_config=ScrapeConfig(),
            status=JobStatus.RUNNING,
            worker_process=mock_process,
        )
        with web_app._jobs._lock:
            web_app._jobs._jobs[busy_job.job_id] = busy_job
            web_app._jobs._active_job_id = busy_job.job_id

        client = TestClient(web_app.app)
        response = client.post(
            "/api/jobs",
            json={"channel_url": "https://www.youtube.com/@example"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("job_id", response.json())
        mock_process.terminate.assert_called_once()
        cancelled = web_app._jobs.get("busy-job")
        self.assertIsNotNone(cancelled)
        assert cancelled is not None
        self.assertEqual(cancelled.status, JobStatus.CANCELLED)
        mock_start.assert_called_once()

    def test_preview_and_download_use_output_file(self) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write("A" * 100 + "\nfull export body")
            path = Path(tmp.name)

        job = JobState(
            job_id="test-job",
            channel_url="https://www.youtube.com/@example",
            filter_config=FilterConfig(),
            export_config=ExportConfig(),
            scrape_config=ScrapeConfig(),
            status=JobStatus.COMPLETED,
            output_path=path,
            channel_label="Example",
            kept_count=1,
        )
        with web_app._jobs._lock:
            web_app._jobs._jobs[job.job_id] = job

        client = TestClient(web_app.app)
        preview = client.get(f"/api/jobs/{job.job_id}/preview")
        self.assertEqual(preview.status_code, 200)
        self.assertTrue(preview.text.startswith("A" * 100))

        download = client.get(f"/api/jobs/{job.job_id}/download")
        self.assertEqual(download.status_code, 200)
        self.assertIn(b"full export body", download.content)

        client.delete(f"/api/jobs/{job.job_id}")
        self.assertFalse(path.exists())

    def test_delete_job_not_found(self) -> None:
        client = TestClient(web_app.app)
        response = client.delete("/api/jobs/missing")
        self.assertEqual(response.status_code, 404)

    @patch("youtube_transcript_api.channel.web.app._start_export_worker")
    @patch("youtube_transcript_api.channel.web.app.proxy_config_label")
    def test_create_job_logs_proxy_hint(
        self, mock_proxy_label, mock_start
    ) -> None:
        mock_proxy_label.return_value = "Using Webshare proxy from environment"
        client = TestClient(web_app.app)
        response = client.post(
            "/api/jobs",
            json={
                "channel_url": "https://www.youtube.com/@example",
                "fetch_config": {"max_workers": 1, "sleep_seconds": 1.0},
            },
        )
        self.assertEqual(response.status_code, 200)
        job_id = response.json()["job_id"]
        job = web_app._jobs.get(job_id)
        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual(job.fetch_max_workers, 1)
        self.assertEqual(job.fetch_sleep_seconds, 1.0)
        self.assertIn("Using Webshare proxy from environment", job.status_log)
        mock_start.assert_called_once()
