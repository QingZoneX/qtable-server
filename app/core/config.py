from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings
from sqlalchemy.engine import URL
from typing import Optional


class Settings(BaseSettings):
    APP_ENV: str = "development"
    PROJECT_NAME: str = "QTable API"
    API_V1_STR: str = "/api/v1"

    # QTable uses the database-backed storage implementation by default.
    # File/JSON storage remains available only when explicitly requested.
    DATA_BACKEND: str = "db"

    # PostgreSQL is the supported default for normal development and deployment.
    # SQLite is an explicit lightweight fallback via DATABASE_MODE=sqlite.
    DATABASE_MODE: str = "postgres"

    # AI
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com/v1"
    DEEPSEEK_MODEL: str = "deepseek-chat"
    OPENAI_BASE_URL: str = "https://api.openai.com/v1"
    OPENAI_MODEL: str = "gpt-4o-mini"
    TOOL_ROUTER_MAX_TOOL_RESULT_CHARS: int = 4000

    # Database
    # QTable reads POSTGRES_* natively. PaaS/Kubernetes environments may inject
    # equivalent values under aliases such as POSTGRESQL_HOST / DB_HOST / PG_HOST.
    POSTGRES_SERVER: str = Field(
        default="localhost",
        validation_alias=AliasChoices(
            "POSTGRES_SERVER",
            "POSTGRESQL_HOST",
            "POSTGRES_HOST",
            "DB_HOST",
            "PG_HOST",
        ),
    )
    POSTGRES_PORT: int = Field(
        default=5432,
        validation_alias=AliasChoices(
            "POSTGRES_PORT",
            "POSTGRESQL_PORT",
            "DB_PORT",
            "PG_PORT",
        ),
    )
    POSTGRES_USER: str = Field(
        default="postgres",
        validation_alias=AliasChoices(
            "POSTGRES_USER",
            "POSTGRESQL_USER",
            "DB_USER",
            "PG_USER",
        ),
    )
    POSTGRES_PASSWORD: str = Field(
        default="postgres",
        validation_alias=AliasChoices(
            "POSTGRES_PASSWORD",
            "POSTGRESQL_PASS",
            "POSTGRESQL_PASSWORD",
            "DB_PASSWORD",
            "PG_PASSWORD",
        ),
    )
    POSTGRES_DB: str = Field(
        default="qtable",
        validation_alias=AliasChoices(
            "POSTGRES_DB",
            "POSTGRESQL_DB",
            "DB_NAME",
            "DB_DATABASE",
            "PG_DB",
        ),
    )
    SQLALCHEMY_DATABASE_URI: Optional[str] = None
    SQLITE_PATH: str = "./qtable.db"
    CREATE_DB_IF_MISSING: bool = True
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_POOL_TIMEOUT: int = 30
    DB_POOL_PRE_PING: bool = True

    # Redis
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0

    # Attachments / S3-compatible object storage.
    # Credentials deliberately have no code defaults. Canonical Compose provides
    # them from .env, while external deployments can point at any compatible S3.
    # The browser never receives storage credentials or persists presigned URLs.
    ATTACHMENT_STORAGE_ENABLED: bool = False
    ATTACHMENT_S3_ENDPOINT: Optional[str] = None
    ATTACHMENT_S3_ACCESS_KEY: Optional[str] = None
    ATTACHMENT_S3_SECRET_KEY: Optional[str] = None
    ATTACHMENT_S3_BUCKET: str = "qtable"
    ATTACHMENT_S3_REGION: Optional[str] = None
    ATTACHMENT_S3_SECURE: bool = False
    ATTACHMENT_MAX_BYTES: int = 50 * 1024 * 1024
    ATTACHMENT_CLEANUP_BATCH_SIZE: int = 100
    ATTACHMENT_CLEANUP_INTERVAL_SECONDS: float = 60.0
    # An upload intent is committed before the object write. Only intents older
    # than this grace are considered abandoned and eligible for object cleanup.
    # Fail closed on dangerously small values instead of allowing immediate
    # cleanup to race a legitimate in-flight upload.
    ATTACHMENT_UPLOAD_PENDING_GRACE_SECONDS: float = Field(default=3600.0, ge=60.0)

    # Automation
    # The durable queue itself is database-backed. This in-process worker is a
    # default consumer; deployments may disable it and run a dedicated worker.
    AUTOMATION_WORKER_ENABLED: bool = True
    AUTOMATION_POLL_SECONDS: float = 5.0
    AUTOMATION_EVENT_BATCH_SIZE: int = 50
    AUTOMATION_RETRY_BATCH_SIZE: int = 50
    AUTOMATION_SCHEDULE_BATCH_SIZE: int = 100

    # Context Engine
    CONTEXT_CACHE_PREFIX: str = "context-engine"
    CONTEXT_CACHE_TTL_SECONDS: int = 300
    CONTEXT_SESSION_TTL_SECONDS: int = 86400
    CONTEXT_MAX_CONVERSATION_MESSAGES: int = 20
    CONTEXT_COMPRESSION_CHAR_BUDGET: int = 6000
    CONTEXT_TABLE_SAMPLE_LIMIT: int = 12

    # Security
    SECRET_KEY: str = "YOUR_SUPER_SECRET_KEY_CHANGE_ME"
    ALGORITHM: str = "HS256"
    JWT_ISSUER: str = "qtable"
    JWT_AUDIENCE: str = "qingzone"
    JWT_DEFAULT_SCOPE: str = "openid profile email qtable"
    # Migration switch: old QTable JWTs did not contain iss/aud/sub/iat.
    # Wrong claims are always rejected; this only permits missing claims.
    JWT_ACCEPT_LEGACY_TOKENS: bool = True
    OAUTH_ALLOW_PLAIN_PKCE: bool = False
    OAUTH_ALLOW_DYNAMIC_CLIENT_REGISTRATION: bool = False
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 480
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30
    RESET_TOKEN_EXPIRE_MINUTES: int = 30
    RESET_PASSWORD_URL_BASE: str = "http://localhost:9100/reset-password"
    # Raw reset tokens are never a production fallback. Development/test may
    # explicitly opt into a debug response field; the default remains safe.
    PASSWORD_RESET_DEBUG_TOKEN_ENABLED: bool = False
    # Password reset abuse controls use Redis in production. Non-production can
    # fall back to a process-local limiter when Redis is unavailable. Limiting
    # keys are target email and token hash; QTable does not trust proxy headers
    # without a separate trusted-proxy contract.
    PASSWORD_RESET_RATE_LIMIT_ENABLED: bool = True
    PASSWORD_RESET_RATE_LIMIT_WINDOW_SECONDS: int = Field(default=900, ge=60, le=86400)
    PASSWORD_RESET_FORGOT_EMAIL_LIMIT: int = Field(default=5, ge=1)
    PASSWORD_RESET_SUBMIT_TOKEN_LIMIT: int = Field(default=10, ge=1)
    QTABLE_WEB_URL: str = "http://localhost:9100"

    # Encryption
    ENCRYPTION_KEY: Optional[str] = None

    SMTP_HOST: Optional[str] = None
    SMTP_PORT: int = 587
    SMTP_USER: Optional[str] = None
    SMTP_PASSWORD: Optional[str] = None
    SMTP_FROM: Optional[str] = None
    SMTP_USE_TLS: bool = True

    class Config:
        env_file = ".env"

    def model_post_init(self, __context):
        if self.SQLALCHEMY_DATABASE_URI is not None:
            return

        mode = (self.DATABASE_MODE or "postgres").lower()
        if mode in {"postgres", "postgresql", "remote", "pg"}:
            self.SQLALCHEMY_DATABASE_URI = URL.create(
                drivername="postgresql+asyncpg",
                username=self.POSTGRES_USER,
                password=self.POSTGRES_PASSWORD,
                host=self.POSTGRES_SERVER,
                port=self.POSTGRES_PORT,
                database=self.POSTGRES_DB,
            ).render_as_string(hide_password=False)
            return

        if mode in {"sqlite", "local"}:
            self.SQLALCHEMY_DATABASE_URI = (
                f"sqlite+aiosqlite:///{self.SQLITE_PATH}"
            )
            return

        raise ValueError(
            "Unsupported DATABASE_MODE. Use 'postgres' (recommended) or 'sqlite'."
        )


settings = Settings()
