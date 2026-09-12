import base64

import pytest
from cryptography.fernet import Fernet

from app.core.config import settings
from app.services.encryption import (
    decrypt_api_key,
    encrypt_api_key,
    get_encryption_key,
)


def test_empty_encryption_key_uses_deterministic_valid_fallback(monkeypatch):
    monkeypatch.setattr(settings, "ENCRYPTION_KEY", None)
    monkeypatch.setattr(settings, "SECRET_KEY", "qtable-encryption-test-secret")

    first = get_encryption_key()
    second = get_encryption_key()

    assert first == second
    assert len(base64.urlsafe_b64decode(first)) == 32
    Fernet(first)

    encrypted = encrypt_api_key("provider-secret")
    assert decrypt_api_key(encrypted) == "provider-secret"


def test_explicit_valid_fernet_key_round_trips(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "ENCRYPTION_KEY", key)

    assert get_encryption_key() == key.encode()
    encrypted = encrypt_api_key("provider-secret")
    assert encrypted != "provider-secret"
    assert decrypt_api_key(encrypted) == "provider-secret"


def test_invalid_explicit_encryption_key_has_actionable_error(monkeypatch):
    monkeypatch.setattr(settings, "ENCRYPTION_KEY", "not-a-fernet-key")

    with pytest.raises(
        ValueError,
        match="ENCRYPTION_KEY 配置无效",
    ):
        encrypt_api_key("provider-secret")


def test_decrypt_with_different_valid_key_requires_resave(monkeypatch):
    first_key = Fernet.generate_key().decode()
    second_key = Fernet.generate_key().decode()

    monkeypatch.setattr(settings, "ENCRYPTION_KEY", first_key)
    encrypted = encrypt_api_key("provider-secret")

    monkeypatch.setattr(settings, "ENCRYPTION_KEY", second_key)
    with pytest.raises(ValueError, match="AI API Key 解密失败，请重新保存配置"):
        decrypt_api_key(encrypted)
