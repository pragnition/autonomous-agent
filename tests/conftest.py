import pytest


@pytest.fixture
def git_env(tmp_path, monkeypatch):
    """A self-contained git identity and global config, so tests never touch the host's."""
    cfg = tmp_path / "gitconfig"
    cfg.write_text("[init]\n\tdefaultBranch = main\n[user]\n\tname = Test\n\temail = test@example.com\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for k, v in {"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
                 "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com"}.items():
        monkeypatch.setenv(k, v)
    return cfg
