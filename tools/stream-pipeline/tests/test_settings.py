import re
from pathlib import Path

import pytest

from wabisanji_post import settings


@pytest.fixture
def no_env(monkeypatch):
    """Settings as a fresh checkout sees them: no .env values, no WS_* variables."""
    monkeypatch.setattr("wabisanji_post.secrets.get", lambda key, default="": default)


def test_defaults_are_portable(no_env):
    home = Path.home()
    paths = [settings.recordings_root(), settings.sessions_root()]
    for p in paths:
        assert p == home / "Videos" or home / "Videos" in p.parents, p
    for p in (settings.content_root(), settings.parser_dir(), settings.layouts_path()):
        assert settings.repo_root() in p.parents or settings.PACKAGE_DIR in p.parents, p
    text = " ".join(map(str, [settings.streamer_name(), settings.twitch_login(), settings.channel_name(),
                              settings.discord_url(), settings.whisper_device(), settings.memes_dir_override()]))
    assert not re.search(r"192\.168\.|\bD:|\bE:", text)
    assert settings.streamer_name() == "the streamer"
    assert settings.whisper_device() == "auto"
    assert settings.family_pattern().search("the kids are here")
    assert settings.local_tz() is not None


def test_env_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("WS_RECORDINGS_ROOT", str(tmp_path))
    monkeypatch.setenv("WS_TIMEZONE", "Australia/Melbourne")
    monkeypatch.setenv("WS_STREAMER_NAME", "Sam")
    monkeypatch.setenv("WS_FAMILY_PATTERN", r"\bjunior\b")
    assert settings.recordings_root() == tmp_path
    assert settings.sessions_root() == tmp_path / "WabiSanji" / "sessions"
    assert str(settings.local_tz()) == "Australia/Melbourne"
    assert settings.streamer_name() == "Sam"
    assert settings.family_pattern().search("Junior joined") and not settings.family_pattern().search("kids")


def test_repo_root_finds_this_checkout():
    assert (settings.repo_root() / "content" / "stream").is_dir()
