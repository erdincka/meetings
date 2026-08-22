"""Configuration parsing.

These guard the properties that broke silently in the old file-backed config:
driver coercion, list parsing from a ConfigMap string, and the absence of any
filesystem access.
"""

from __future__ import annotations

from app.core.config import Settings


class TestDatabaseUrlNormalization:
    def test_bare_postgresql_is_coerced_to_asyncpg(self) -> None:
        s = Settings(DATABASE_URL="postgresql://u:p@host:5432/db")
        assert s.DATABASE_URL == "postgresql+asyncpg://u:p@host:5432/db"

    def test_heroku_style_postgres_scheme_is_coerced(self) -> None:
        s = Settings(DATABASE_URL="postgres://u:p@host:5432/db")
        assert s.DATABASE_URL == "postgresql+asyncpg://u:p@host:5432/db"

    def test_already_asyncpg_is_left_alone(self) -> None:
        url = "postgresql+asyncpg://u:p@host:5432/db"
        assert Settings(DATABASE_URL=url).DATABASE_URL == url

    def test_password_containing_scheme_text_is_not_mangled(self) -> None:
        # CNPG generates random passwords; one containing "postgresql://" must
        # not trigger a second substitution.
        url = "postgresql://u:xpostgresql://y@host:5432/db"
        assert Settings(DATABASE_URL=url).DATABASE_URL == (
            "postgresql+asyncpg://u:xpostgresql://y@host:5432/db"
        )

    def test_none_is_preserved(self) -> None:
        assert Settings(DATABASE_URL=None).DATABASE_URL is None


class TestAllowedOrigins:
    def test_comma_separated_string_is_split(self) -> None:
        s = Settings(ALLOWED_ORIGINS="http://a.test, http://b.test")
        assert s.ALLOWED_ORIGINS == ["http://a.test", "http://b.test"]

    def test_blank_entries_are_dropped(self) -> None:
        s = Settings(ALLOWED_ORIGINS="http://a.test,,  ,http://b.test")
        assert s.ALLOWED_ORIGINS == ["http://a.test", "http://b.test"]

    def test_list_passes_through(self) -> None:
        assert Settings(ALLOWED_ORIGINS=["http://a.test"]).ALLOWED_ORIGINS == ["http://a.test"]

    def test_wildcard_with_credentials_is_not_the_default(self) -> None:
        # Regression guard: the old app shipped allow_origins=["*"] together
        # with allow_credentials=True, which the CORS spec rejects outright.
        assert "*" not in Settings().ALLOWED_ORIGINS


class TestConfiguredPredicates:
    """Both halves are required. Built from a cleared environment, since
    conftest populates these vars for the rest of the suite."""

    @staticmethod
    def _clean(monkeypatch, **kwargs: object) -> Settings:
        for var in (
            "INFERENCE_ENDPOINT",
            "INFERENCE_MODEL_NAME",
            "EMBEDDING_ENDPOINT",
            "EMBEDDING_MODEL_NAME",
        ):
            monkeypatch.delenv(var, raising=False)
        return Settings(_env_file=None, **kwargs)

    def test_inference_needs_both_endpoint_and_model(self, monkeypatch) -> None:
        assert not self._clean(monkeypatch, INFERENCE_ENDPOINT="http://x/v1").inference_configured
        assert not self._clean(monkeypatch, INFERENCE_MODEL_NAME="m").inference_configured
        assert self._clean(
            monkeypatch, INFERENCE_ENDPOINT="http://x/v1", INFERENCE_MODEL_NAME="m"
        ).inference_configured

    def test_embedding_needs_both_endpoint_and_model(self, monkeypatch) -> None:
        assert not self._clean(monkeypatch, EMBEDDING_ENDPOINT="http://x/v1").embedding_configured
        assert self._clean(
            monkeypatch, EMBEDDING_ENDPOINT="http://x/v1", EMBEDDING_MODEL_NAME="m"
        ).embedding_configured


def test_config_module_performs_no_file_io(monkeypatch) -> None:
    """The whole point of Phase 1: settings access must not touch the disk."""
    import builtins

    real_open = builtins.open
    opened: list[str] = []

    def tracking_open(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        opened.append(str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", tracking_open)

    s = Settings(_env_file=None)
    _ = s.DATABASE_URL, s.INFERENCE_API_KEY, s.inference_configured, s.ALLOWED_ORIGINS

    assert opened == [], f"settings access opened files: {opened}"
