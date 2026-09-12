import pytest

from app.core.config import Settings


DATABASE_ENV_NAMES = (
    "DATA_BACKEND",
    "DATABASE_MODE",
    "SQLALCHEMY_DATABASE_URI",
    "POSTGRES_SERVER",
    "POSTGRES_HOST",
    "POSTGRESQL_HOST",
    "DB_HOST",
    "PG_HOST",
    "POSTGRES_PORT",
    "POSTGRESQL_PORT",
    "DB_PORT",
    "PG_PORT",
    "POSTGRES_USER",
    "POSTGRESQL_USER",
    "DB_USER",
    "PG_USER",
    "POSTGRES_PASSWORD",
    "POSTGRESQL_PASS",
    "POSTGRESQL_PASSWORD",
    "DB_PASSWORD",
    "PG_PASSWORD",
    "POSTGRES_DB",
    "POSTGRESQL_DB",
    "DB_NAME",
    "DB_DATABASE",
    "PG_DB",
    "SQLITE_PATH",
)


def _clear_database_env(monkeypatch):
    for name in DATABASE_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_default_storage_backend_is_postgres(monkeypatch):
    _clear_database_env(monkeypatch)

    settings = Settings(_env_file=None)

    assert settings.DATA_BACKEND == "db"
    assert settings.DATABASE_MODE == "postgres"
    assert settings.SQLALCHEMY_DATABASE_URI == (
        "postgresql+asyncpg://postgres:postgres@localhost:5432/qtable"
    )


def test_postgres_mode_accepts_paas_aliases(monkeypatch):
    _clear_database_env(monkeypatch)
    monkeypatch.setenv("POSTGRESQL_HOST", "postgres.internal")
    monkeypatch.setenv("POSTGRESQL_PORT", "55432")
    monkeypatch.setenv("POSTGRESQL_USER", "qtable_user")
    monkeypatch.setenv("POSTGRESQL_PASS", "qtable_password")
    monkeypatch.setenv("POSTGRESQL_DB", "qtable_dev")

    settings = Settings(_env_file=None)

    assert settings.SQLALCHEMY_DATABASE_URI == (
        "postgresql+asyncpg://qtable_user:qtable_password"
        "@postgres.internal:55432/qtable_dev"
    )


def test_postgres_uri_escapes_special_characters(monkeypatch):
    _clear_database_env(monkeypatch)
    monkeypatch.setenv("POSTGRES_PASSWORD", "p@ss:word/with#chars")

    settings = Settings(_env_file=None)

    assert "p%40ss%3Aword%2Fwith%23chars" in settings.SQLALCHEMY_DATABASE_URI


def test_explicit_sqlite_mode_uses_sqlite_even_with_postgres_values(monkeypatch):
    _clear_database_env(monkeypatch)
    monkeypatch.setenv("DATABASE_MODE", "sqlite")
    monkeypatch.setenv("SQLITE_PATH", "/tmp/qtable-local.db")
    monkeypatch.setenv("POSTGRES_SERVER", "postgres.internal")
    monkeypatch.setenv("POSTGRES_USER", "qtable_user")
    monkeypatch.setenv("POSTGRES_DB", "qtable_dev")

    settings = Settings(_env_file=None)

    assert settings.SQLALCHEMY_DATABASE_URI == (
        "sqlite+aiosqlite:////tmp/qtable-local.db"
    )


def test_invalid_database_mode_is_rejected(monkeypatch):
    _clear_database_env(monkeypatch)
    monkeypatch.setenv("DATABASE_MODE", "auto")

    with pytest.raises(ValueError, match="Unsupported DATABASE_MODE"):
        Settings(_env_file=None)
