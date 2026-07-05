"""Tests for EvolutionConfig lazy repo discovery."""

import pytest

from evolution.core.config import EvolutionConfig, discover_hermes_agent_path


class TestLazyDiscovery:
    def test_config_constructs_without_repo(self, monkeypatch, tmp_path):
        """Constructing a config must never crash — offline paths (constraint
        validation, digests, tests) don't need a hermes-agent checkout."""
        monkeypatch.delenv("HERMES_AGENT_REPO", raising=False)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        config = EvolutionConfig()
        assert config.hermes_agent_path is None

    def test_require_raises_when_missing(self, monkeypatch, tmp_path):
        monkeypatch.delenv("HERMES_AGENT_REPO", raising=False)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        config = EvolutionConfig()
        with pytest.raises(FileNotFoundError):
            config.require_hermes_agent_path()

    def test_env_var_wins(self, monkeypatch, tmp_path):
        repo = tmp_path / "hermes-agent"
        repo.mkdir()
        monkeypatch.setenv("HERMES_AGENT_REPO", str(repo))
        config = EvolutionConfig()
        assert config.hermes_agent_path == repo
        assert config.require_hermes_agent_path() == repo

    def test_home_install_discovered(self, monkeypatch, tmp_path):
        monkeypatch.delenv("HERMES_AGENT_REPO", raising=False)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        install = tmp_path / ".hermes" / "hermes-agent"
        install.mkdir(parents=True)
        assert discover_hermes_agent_path() == install

    def test_nonexistent_env_path_falls_through(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_AGENT_REPO", str(tmp_path / "does-not-exist"))
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        assert discover_hermes_agent_path() is None
