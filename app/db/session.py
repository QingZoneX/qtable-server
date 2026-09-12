from typing import Optional
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from app.core.config import settings

engine_kwargs = {"echo": False}
if (settings.SQLALCHEMY_DATABASE_URI or "").startswith("postgresql"):
    engine_kwargs.update(
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_timeout=settings.DB_POOL_TIMEOUT,
        pool_pre_ping=settings.DB_POOL_PRE_PING,
    )
engine = create_async_engine(settings.SQLALCHEMY_DATABASE_URI, **engine_kwargs)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# Register global ORM write invariants after the session factory is defined.
# The listener itself has no dependency on AsyncSessionLocal, avoiding a cycle.
from app.services import attachment_invariants as _attachment_invariants  # noqa: E402,F401


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
