from unittest import TestCase
from unittest.mock import patch

from youtube_transcript_api.channel.proxy import (
    proxy_config_from_env,
    proxy_config_label,
    resolve_proxy_config,
)
from youtube_transcript_api.proxies import GenericProxyConfig, WebshareProxyConfig


class TestChannelProxy(TestCase):
    def test_proxy_config_from_env_webshare(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "WEBSHARE_PROXY_USERNAME": "user",
                "WEBSHARE_PROXY_PASSWORD": "pass",
            },
            clear=True,
        ):
            config = proxy_config_from_env()
        self.assertIsInstance(config, WebshareProxyConfig)
        self.assertEqual(config.proxy_username, "user")
        self.assertEqual(config.proxy_password, "pass")
        self.assertEqual(config.retries_when_blocked, 2)

    def test_proxy_config_webshare_retries_from_env(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "WEBSHARE_PROXY_USERNAME": "user",
                "WEBSHARE_PROXY_PASSWORD": "pass",
                "WEBSHARE_PROXY_RETRIES": "0",
            },
            clear=True,
        ):
            config = proxy_config_from_env()
        self.assertIsInstance(config, WebshareProxyConfig)
        self.assertEqual(config.retries_when_blocked, 0)

    def test_proxy_config_from_env_http_proxy(self) -> None:
        with patch.dict(
            "os.environ",
            {"HTTP_PROXY": "http://proxy.example:8080"},
            clear=True,
        ):
            config = proxy_config_from_env()
        self.assertIsInstance(config, GenericProxyConfig)
        self.assertEqual(config.http_url, "http://proxy.example:8080")

    def test_proxy_config_label_webshare(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "WEBSHARE_PROXY_USERNAME": "user",
                "WEBSHARE_PROXY_PASSWORD": "pass",
            },
            clear=True,
        ):
            label = proxy_config_label()
        self.assertEqual(label, "Using Webshare proxy from environment")

    def test_resolve_proxy_config_cli_overrides_env(self) -> None:
        with patch.dict(
            "os.environ",
            {"HTTP_PROXY": "http://ignored:1"},
            clear=True,
        ):
            config = resolve_proxy_config(http_proxy="http://cli:9")
        self.assertIsInstance(config, GenericProxyConfig)
        self.assertEqual(config.http_url, "http://cli:9")

    @patch("youtube_transcript_api.channel.cli.run_pipeline")
    @patch("youtube_transcript_api.channel.cli.validate_channel_url")
    def test_cli_passes_proxy_config(self, mock_validate, mock_run_pipeline) -> None:
        from unittest.mock import MagicMock

        from youtube_transcript_api.channel.cli import main
        from youtube_transcript_api.channel.models import PipelineResult

        mock_validate.return_value = None
        mock_run_pipeline.return_value = MagicMock(
            result=PipelineResult(channel_label="Test", filter_summary=""),
        )

        exit_code = main(
            [
                "https://www.youtube.com/@example",
                "--http-proxy",
                "http://proxy:8080",
                "-o",
                "out.txt",
            ]
        )

        self.assertEqual(exit_code, 0)
        proxy_config = mock_run_pipeline.call_args.kwargs["proxy_config"]
        self.assertIsInstance(proxy_config, GenericProxyConfig)
        self.assertEqual(proxy_config.http_url, "http://proxy:8080")
