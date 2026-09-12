import base64
import binascii
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings


INVALID_ENCRYPTION_KEY_MESSAGE = (
    "ENCRYPTION_KEY 配置无效：必须使用 cryptography.fernet.Fernet.generate_key() "
    "生成 32 字节 URL-safe Base64 密钥；不要填写普通密码、占位符或任意 32 位字符串。"
)


def get_encryption_key() -> bytes:
    """Return the configured Fernet key or the deterministic dev fallback."""
    key = settings.ENCRYPTION_KEY
    if key:
        encoded = key.encode()
        try:
            # Validate explicit operator configuration before an AI credential
            # is written so cryptography internals never leak through GraphQL.
            Fernet(encoded)
        except (ValueError, TypeError, binascii.Error) as exc:
            raise ValueError(INVALID_ENCRYPTION_KEY_MESSAGE) from exc
        return encoded

    # Fall back to a deterministic key so saved API keys remain decryptable
    # across restarts in local development.
    return base64.urlsafe_b64encode(
        hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()
    )


def encrypt_api_key(api_key: str) -> str:
    """Encrypt a provider API key for database storage."""
    fernet = Fernet(get_encryption_key())
    encrypted = fernet.encrypt(api_key.encode())
    return encrypted.decode()


def decrypt_api_key(encrypted_key: str) -> str:
    """Decrypt a provider API key from database storage."""
    fernet = Fernet(get_encryption_key())
    try:
        decrypted = fernet.decrypt(encrypted_key.encode())
    except InvalidToken as exc:
        raise ValueError("AI API Key 解密失败，请重新保存配置。") from exc
    return decrypted.decode()
