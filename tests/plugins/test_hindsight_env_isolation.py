"""Third-party memory imports must not rewrite the Hermes process environment.

``hindsight_api.config`` executes, at module import time::

    load_dotenv(find_dotenv(usecwd=True), override=True)

and ``from hindsight import HindsightEmbedded`` pulls it in transitively.
``find_dotenv`` walks up from the *process cwd*, which for a Hermes gateway is
the hermes-agent checkout — so it resolves to the default profile's
``~/.hermes/.env`` no matter which profile is running, and ``override=True``
then replaces variables the process already loaded from its own
``$HERMES_HOME/.env``.

That is not confined to Hindsight's own settings: a profile ``.env`` also holds
``DISCORD_BOT_TOKEN``, ``DISCORD_ALLOWED_CHANNELS``,
``DISCORD_FREE_RESPONSE_CHANNELS`` and ``MATRIX_ACCESS_TOKEN``. Memory
initializes lazily, on the first turn that uses it, so a non-default profile
answers exactly one message and then goes permanently silent: every later
message is dropped by a channel allow-list that now belongs to another bot.
"""

import importlib
import os
import sys
import types

import pytest

hindsight = importlib.import_module("plugins.memory.hindsight")


class TestPreservedProcessEnv:
    def test_restores_an_overwritten_value(self, monkeypatch):
        monkeypatch.setenv("DISCORD_ALLOWED_CHANNELS", "mine")
        with hindsight._preserved_process_env("fake"):
            os.environ["DISCORD_ALLOWED_CHANNELS"] = "someone-elses"
        assert os.environ["DISCORD_ALLOWED_CHANNELS"] == "mine"

    def test_removes_a_key_the_body_added(self, monkeypatch):
        monkeypatch.delenv("HERMES_TEST_ONLY_KEY", raising=False)
        with hindsight._preserved_process_env("fake"):
            os.environ["HERMES_TEST_ONLY_KEY"] = "leaked"
        assert "HERMES_TEST_ONLY_KEY" not in os.environ

    def test_restores_a_key_the_body_deleted(self, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "oracle-token")
        with hindsight._preserved_process_env("fake"):
            del os.environ["DISCORD_BOT_TOKEN"]
        assert os.environ["DISCORD_BOT_TOKEN"] == "oracle-token"

    def test_restores_even_when_the_body_raises(self, monkeypatch):
        monkeypatch.setenv("DISCORD_ALLOWED_CHANNELS", "mine")
        with pytest.raises(RuntimeError):
            with hindsight._preserved_process_env("fake"):
                os.environ["DISCORD_ALLOWED_CHANNELS"] = "someone-elses"
                raise RuntimeError("import blew up")
        assert os.environ["DISCORD_ALLOWED_CHANNELS"] == "mine"

    def test_quiet_when_nothing_changed(self, monkeypatch, caplog):
        monkeypatch.setenv("DISCORD_ALLOWED_CHANNELS", "mine")
        with caplog.at_level("INFO", logger=hindsight.logger.name):
            with hindsight._preserved_process_env("fake"):
                pass
        assert not caplog.records

    def test_logs_what_it_restored(self, monkeypatch, caplog):
        monkeypatch.setenv("DISCORD_ALLOWED_CHANNELS", "mine")
        with caplog.at_level("INFO", logger=hindsight.logger.name):
            with hindsight._preserved_process_env("fake-runtime"):
                os.environ["DISCORD_ALLOWED_CHANNELS"] = "someone-elses"
        assert any(
            "fake-runtime" in r.getMessage() and "DISCORD_ALLOWED_CHANNELS" in r.getMessage()
            for r in caplog.records
        )


class TestImportHindsightEmbedded:
    """The real seam: ``from hindsight import HindsightEmbedded`` is guarded."""

    @staticmethod
    def _install_env_clobbering_hindsight(monkeypatch):
        """Stand in for the real package, reproducing its import side effect.

        A module-level ``__getattr__`` (PEP 562) fires on the ``from ... import``
        lookup, which is the same point in the guarded block where the real
        package's ``load_dotenv(override=True)`` runs.
        """

        module = types.ModuleType("hindsight")

        class HindsightEmbedded:
            pass

        def __getattr__(name):
            if name != "HindsightEmbedded":
                raise AttributeError(name)
            os.environ["DISCORD_BOT_TOKEN"] = "default-profile-token"
            os.environ["DISCORD_ALLOWED_CHANNELS"] = "111,222"
            return HindsightEmbedded

        module.__getattr__ = __getattr__
        monkeypatch.setitem(sys.modules, "hindsight", module)
        return HindsightEmbedded

    def test_returns_the_class(self, monkeypatch):
        expected = self._install_env_clobbering_hindsight(monkeypatch)
        assert hindsight._import_hindsight_embedded() is expected

    def test_import_does_not_leak_the_foreign_env(self, monkeypatch):
        self._install_env_clobbering_hindsight(monkeypatch)
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "oracle-token")
        monkeypatch.setenv("DISCORD_ALLOWED_CHANNELS", "999")

        hindsight._import_hindsight_embedded()

        assert os.environ["DISCORD_BOT_TOKEN"] == "oracle-token"
        assert os.environ["DISCORD_ALLOWED_CHANNELS"] == "999"
