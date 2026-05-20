import os

from youtube_transcript_api.channel.env import load_local_env


def test_load_local_env_prefers_shell_over_files(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("YOUTUBE_API_KEY=from-env\n", encoding="utf-8")
    local_file = tmp_path / ".env.local"
    local_file.write_text("YOUTUBE_API_KEY=from-local\n", encoding="utf-8")

    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    load_local_env(tmp_path)
    assert os.environ["YOUTUBE_API_KEY"] == "from-local"


def test_load_local_env_does_not_override_shell(tmp_path, monkeypatch):
    (tmp_path / ".env.local").write_text(
        "YOUTUBE_API_KEY=from-file\n", encoding="utf-8"
    )
    monkeypatch.setenv("YOUTUBE_API_KEY", "from-shell")
    load_local_env(tmp_path)
    assert os.environ["YOUTUBE_API_KEY"] == "from-shell"
