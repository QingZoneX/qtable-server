from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Optional
from urllib import error as urllib_error
from urllib import request as urllib_request

import redis.asyncio as redis
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.skill_registry import (
    SkillCategory,
    SkillEmbedding,
    SkillRegistryEntry,
    SkillVersion,
)
from app.skills.contracts import (
    SkillManifestEntry,
    SkillMetadata,
    SkillPermissionRequirement,
)
from app.skills.runtime import SkillDefinition, SkillRegistry, build_default_registry


class GenericSkillPayload(BaseModel):
    model_config = ConfigDict(extra="allow")


class SkillRegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    version: str = "1.0.0"
    title: str
    description: str
    category: str = "general"
    namespace: Optional[str] = None
    workspace_id: Optional[str] = Field(default=None, alias="workspaceId")
    tags: list[str] = Field(default_factory=list)
    visibility: Literal["internal", "workspace", "marketplace"] = "internal"
    source_type: Literal["builtin", "workspace", "marketplace", "plugin"] = "plugin"
    runtime_kind: Literal[
        "builtin",
        "python_module",
        "http_proxy",
        "mcp",
        "openai_tool",
    ] = "python_module"
    side_effect: Literal["none", "write", "external"] = "none"
    confirmation_required: bool = False
    idempotent: bool = True
    supports_dry_run: bool = True
    entrypoint: Optional[str] = None
    module_path: Optional[str] = Field(default=None, alias="modulePath")
    handler_name: Optional[str] = Field(default=None, alias="handlerName")
    icon: Optional[str] = None
    input_schema: dict[str, Any] = Field(default_factory=dict, alias="inputSchema")
    output_schema: dict[str, Any] = Field(default_factory=dict, alias="outputSchema")
    permissions: list[SkillPermissionRequirement] = Field(default_factory=list)
    transport_config: dict[str, Any] = Field(default_factory=dict, alias="transportConfig")
    openai_tool_schema: Optional[dict[str, Any]] = Field(default=None, alias="openaiToolSchema")
    mcp_tool_schema: Optional[dict[str, Any]] = Field(default=None, alias="mcpToolSchema")
    cache_ttl_seconds: int = Field(default=300, ge=30, le=3600, alias="cacheTtlSeconds")
    embedding_text: Optional[str] = Field(default=None, alias="embeddingText")
    changelog: Optional[str] = None


class SkillStatusUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_enabled: bool = Field(..., alias="isEnabled")
    status: Literal["draft", "active", "disabled", "deprecated"] = "active"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return slug or "general"


def _stable_hash(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib_request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib_request.urlopen(req, timeout=15) as response:
        raw = response.read().decode("utf-8")
        if not raw:
            return {}
        return json.loads(raw)


class SkillRegistryService:
    CACHE_PREFIX = "skill-registry"

    def __init__(self) -> None:
        self._redis_client: redis.Redis | None = None

    async def _get_redis(self) -> redis.Redis | None:
        if self._redis_client is not None:
            return self._redis_client
        try:
            client = redis.Redis(
                host=settings.REDIS_HOST,
                port=settings.REDIS_PORT,
                decode_responses=True,
                socket_connect_timeout=0.3,
                socket_timeout=0.5,
            )
            await client.ping()
            self._redis_client = client
            return client
        except Exception:
            return None

    async def _get_cached_json(self, key: str) -> Any | None:
        client = await self._get_redis()
        if client is None:
            return None
        try:
            value = await client.get(key)
            return json.loads(value) if value else None
        except Exception:
            return None

    async def _set_cached_json(self, key: str, value: Any, ttl_seconds: int) -> None:
        client = await self._get_redis()
        if client is None:
            return
        try:
            await client.setex(key, ttl_seconds, json.dumps(value, ensure_ascii=True))
        except Exception:
            return

    async def invalidate_registry_cache(self) -> None:
        client = await self._get_redis()
        if client is None:
            return
        cursor = 0
        pattern = f"{self.CACHE_PREFIX}:*"
        try:
            while True:
                cursor, keys = await client.scan(cursor=cursor, match=pattern, count=100)
                if keys:
                    await client.delete(*keys)
                if cursor == 0:
                    break
        except Exception:
            return

    def _build_metadata(self, payload: SkillRegistrationRequest) -> SkillMetadata:
        return SkillMetadata(
            name=payload.name,
            version=payload.version,
            title=payload.title,
            description=payload.description,
            tags=payload.tags,
            side_effect=payload.side_effect,
            confirmation_required=payload.confirmation_required,
            idempotent=payload.idempotent,
            supports_dry_run=payload.supports_dry_run,
            visibility=payload.visibility,
            permissions=payload.permissions,
        )

    def _build_manifest(self, payload: SkillRegistrationRequest) -> SkillManifestEntry:
        return SkillManifestEntry(
            metadata=self._build_metadata(payload),
            input_schema=payload.input_schema,
            output_schema=payload.output_schema,
        )

    async def _ensure_category(self, db: AsyncSession, category_name: str) -> SkillCategory:
        slug = _slugify(category_name)
        result = await db.execute(select(SkillCategory).where(SkillCategory.slug == slug).limit(1))
        category = result.scalars().first()
        if category:
            if not category.name:
                category.name = category_name
            return category
        category = SkillCategory(
            id=str(uuid.uuid4()),
            slug=slug,
            name=category_name or "General",
            description=f"Skills grouped under {category_name or 'General'}",
            sort_order=0,
            is_enabled=True,
        )
        db.add(category)
        await db.flush()
        return category

    async def sync_builtin_registry_entries(self, db: AsyncSession) -> None:
        registry = build_default_registry()
        for manifest in registry.list_manifest():
            metadata = manifest.metadata
            category_name = metadata.tags[0] if metadata.tags else "general"
            category = await self._ensure_category(db, category_name.title())
            result = await db.execute(
                select(SkillRegistryEntry)
                .where(
                    SkillRegistryEntry.canonical_name == metadata.name,
                    SkillRegistryEntry.workspace_id.is_(None),
                )
                .limit(1)
            )
            entry = result.scalars().first()
            payload = {
                "metadata": metadata.model_dump(mode="json"),
                "input_schema": manifest.input_schema,
                "output_schema": manifest.output_schema,
            }
            embedding_text = f"{metadata.title}\n{metadata.description}\n{' '.join(metadata.tags)}"
            if entry is None:
                entry = SkillRegistryEntry(
                    id=str(uuid.uuid4()),
                    workspace_id=None,
                    category_id=category.id,
                    canonical_name=metadata.name,
                    namespace=metadata.name.split(".", 1)[0] if "." in metadata.name else None,
                    latest_version=metadata.version,
                    title=metadata.title,
                    description=metadata.description,
                    tags=metadata.tags,
                    visibility=metadata.visibility,
                    source_type="builtin",
                    runtime_kind="builtin",
                    status="active",
                    is_enabled=True,
                    manifest_json=payload,
                    input_schema=manifest.input_schema,
                    output_schema=manifest.output_schema,
                    permissions_json=[
                        item.model_dump(mode="json") for item in metadata.permissions
                    ],
                    openai_tool_schema=self._build_openai_tool_schema(manifest),
                    mcp_tool_schema=self._build_mcp_tool_schema(manifest),
                    transport_config={},
                    cache_ttl_seconds=300,
                    embedding_status="pending",
                    embedding_text=embedding_text,
                    created_by="system",
                )
                db.add(entry)
                await db.flush()
            else:
                entry.category_id = category.id
                entry.latest_version = metadata.version
                entry.title = metadata.title
                entry.description = metadata.description
                entry.tags = metadata.tags
                entry.visibility = metadata.visibility
                entry.source_type = "builtin"
                entry.runtime_kind = "builtin"
                entry.status = "active"
                entry.is_enabled = True
                entry.manifest_json = payload
                entry.input_schema = manifest.input_schema
                entry.output_schema = manifest.output_schema
                entry.permissions_json = [
                    item.model_dump(mode="json") for item in metadata.permissions
                ]
                entry.openai_tool_schema = self._build_openai_tool_schema(manifest)
                entry.mcp_tool_schema = self._build_mcp_tool_schema(manifest)
                entry.transport_config = {}
                entry.embedding_text = embedding_text

            await self._upsert_version(
                db=db,
                entry=entry,
                version=metadata.version,
                changelog="Builtin skill sync",
                manifest_json=payload,
                input_schema=manifest.input_schema,
                output_schema=manifest.output_schema,
                permissions_json=[item.model_dump(mode="json") for item in metadata.permissions],
                openai_tool_schema=self._build_openai_tool_schema(manifest),
                mcp_tool_schema=self._build_mcp_tool_schema(manifest),
                transport_config={},
                embedding_text=embedding_text,
            )
        await db.commit()
        await self.invalidate_registry_cache()

    async def _upsert_version(
        self,
        *,
        db: AsyncSession,
        entry: SkillRegistryEntry,
        version: str,
        changelog: Optional[str],
        manifest_json: dict[str, Any],
        input_schema: dict[str, Any],
        output_schema: dict[str, Any],
        permissions_json: list[dict[str, Any]],
        openai_tool_schema: Optional[dict[str, Any]],
        mcp_tool_schema: Optional[dict[str, Any]],
        transport_config: dict[str, Any],
        embedding_text: str,
    ) -> SkillVersion:
        result = await db.execute(
            select(SkillVersion)
            .where(SkillVersion.skill_id == entry.id, SkillVersion.version == version)
            .limit(1)
        )
        version_row = result.scalars().first()
        checksum = _stable_hash(
            {
                "manifest": manifest_json,
                "input_schema": input_schema,
                "output_schema": output_schema,
                "transport": transport_config,
            }
        )
        await db.execute(
            select(SkillVersion).where(SkillVersion.skill_id == entry.id)
        )
        result_all = await db.execute(select(SkillVersion).where(SkillVersion.skill_id == entry.id))
        for item in result_all.scalars().all():
            item.is_current = False
        if version_row is None:
            version_row = SkillVersion(
                id=str(uuid.uuid4()),
                skill_id=entry.id,
                version=version,
                changelog=changelog,
                checksum=checksum,
                is_current=True,
                manifest_json=manifest_json,
                input_schema=input_schema,
                output_schema=output_schema,
                permissions_json=permissions_json,
                openai_tool_schema=openai_tool_schema,
                mcp_tool_schema=mcp_tool_schema,
                transport_config=transport_config,
                embedding_text=embedding_text,
                published_at=_now_utc(),
            )
            db.add(version_row)
            await db.flush()
        else:
            version_row.changelog = changelog
            version_row.checksum = checksum
            version_row.is_current = True
            version_row.manifest_json = manifest_json
            version_row.input_schema = input_schema
            version_row.output_schema = output_schema
            version_row.permissions_json = permissions_json
            version_row.openai_tool_schema = openai_tool_schema
            version_row.mcp_tool_schema = mcp_tool_schema
            version_row.transport_config = transport_config
            version_row.embedding_text = embedding_text
            version_row.published_at = _now_utc()
        await self._queue_embedding_placeholder(db, version_row, embedding_text)
        return version_row

    async def _queue_embedding_placeholder(
        self,
        db: AsyncSession,
        version_row: SkillVersion,
        embedding_text: str,
    ) -> None:
        content_hash = _stable_hash({"text": embedding_text})
        result = await db.execute(
            select(SkillEmbedding)
            .where(
                SkillEmbedding.skill_version_id == version_row.id,
                SkillEmbedding.content_hash == content_hash,
            )
            .limit(1)
        )
        embedding = result.scalars().first()
        if embedding:
            return
        db.add(
            SkillEmbedding(
                id=str(uuid.uuid4()),
                skill_version_id=version_row.id,
                provider="pending",
                model="pending",
                vector_dim=0,
                embedding=None,
                content_hash=content_hash,
                indexed_at=None,
            )
        )

    async def register_skill(
        self,
        db: AsyncSession,
        payload: SkillRegistrationRequest,
        *,
        actor: str | None = None,
    ) -> dict[str, Any]:
        category = await self._ensure_category(db, payload.category.title())
        manifest = self._build_manifest(payload)
        manifest_json = {
            "metadata": manifest.metadata.model_dump(mode="json"),
            "input_schema": manifest.input_schema,
            "output_schema": manifest.output_schema,
        }
        result = await db.execute(
            select(SkillRegistryEntry)
            .where(
                SkillRegistryEntry.canonical_name == payload.name,
                SkillRegistryEntry.workspace_id == payload.workspace_id,
            )
            .limit(1)
        )
        entry = result.scalars().first()
        openai_tool_schema = payload.openai_tool_schema or self._build_openai_tool_schema(manifest)
        mcp_tool_schema = payload.mcp_tool_schema or self._build_mcp_tool_schema(manifest)
        permissions_json = [
            permission.model_dump(mode="json") for permission in payload.permissions
        ]
        embedding_text = payload.embedding_text or (
            f"{payload.title}\n{payload.description}\n{' '.join(payload.tags)}"
        )
        if entry is None:
            entry = SkillRegistryEntry(
                id=str(uuid.uuid4()),
                workspace_id=payload.workspace_id,
                category_id=category.id,
                canonical_name=payload.name,
                namespace=payload.namespace or payload.name.split(".", 1)[0],
                latest_version=payload.version,
                title=payload.title,
                description=payload.description,
                tags=payload.tags,
                visibility=payload.visibility,
                source_type=payload.source_type,
                runtime_kind=payload.runtime_kind,
                status="active",
                is_enabled=True,
                entrypoint=payload.entrypoint,
                module_path=payload.module_path,
                handler_name=payload.handler_name,
                icon=payload.icon,
                manifest_json=manifest_json,
                input_schema=payload.input_schema,
                output_schema=payload.output_schema,
                permissions_json=permissions_json,
                openai_tool_schema=openai_tool_schema,
                mcp_tool_schema=mcp_tool_schema,
                transport_config=payload.transport_config,
                cache_ttl_seconds=payload.cache_ttl_seconds,
                embedding_status="pending",
                embedding_text=embedding_text,
                created_by=actor,
                started_at=_now_utc(),
            )
            db.add(entry)
            await db.flush()
        else:
            entry.category_id = category.id
            entry.namespace = payload.namespace or entry.namespace
            entry.latest_version = payload.version
            entry.title = payload.title
            entry.description = payload.description
            entry.tags = payload.tags
            entry.visibility = payload.visibility
            entry.source_type = payload.source_type
            entry.runtime_kind = payload.runtime_kind
            entry.entrypoint = payload.entrypoint
            entry.module_path = payload.module_path
            entry.handler_name = payload.handler_name
            entry.icon = payload.icon
            entry.manifest_json = manifest_json
            entry.input_schema = payload.input_schema
            entry.output_schema = payload.output_schema
            entry.permissions_json = permissions_json
            entry.openai_tool_schema = openai_tool_schema
            entry.mcp_tool_schema = mcp_tool_schema
            entry.transport_config = payload.transport_config
            entry.cache_ttl_seconds = payload.cache_ttl_seconds
            entry.embedding_status = "pending"
            entry.embedding_text = embedding_text
            if not entry.started_at:
                entry.started_at = _now_utc()
            entry.stopped_at = None
            entry.is_enabled = True
            entry.status = "active"

        await self._upsert_version(
            db=db,
            entry=entry,
            version=payload.version,
            changelog=payload.changelog,
            manifest_json=manifest_json,
            input_schema=payload.input_schema,
            output_schema=payload.output_schema,
            permissions_json=permissions_json,
            openai_tool_schema=openai_tool_schema,
            mcp_tool_schema=mcp_tool_schema,
            transport_config=payload.transport_config,
            embedding_text=embedding_text,
        )
        await db.commit()
        await self.invalidate_registry_cache()
        return self.serialize_registry_entry(entry, category)

    async def set_skill_status(
        self,
        db: AsyncSession,
        *,
        skill_id: str,
        payload: SkillStatusUpdateRequest,
    ) -> dict[str, Any] | None:
        result = await db.execute(
            select(SkillRegistryEntry).where(SkillRegistryEntry.id == skill_id).limit(1)
        )
        entry = result.scalars().first()
        if entry is None:
            return None
        entry.is_enabled = payload.is_enabled
        entry.status = payload.status
        if payload.is_enabled:
            entry.started_at = _now_utc()
            entry.stopped_at = None
        else:
            entry.stopped_at = _now_utc()
            if payload.status == "active":
                entry.status = "disabled"
        category = await self.get_category_by_id(db, entry.category_id)
        await db.commit()
        await self.invalidate_registry_cache()
        return self.serialize_registry_entry(entry, category)

    async def get_category_by_id(
        self,
        db: AsyncSession,
        category_id: str | None,
    ) -> SkillCategory | None:
        if not category_id:
            return None
        result = await db.execute(
            select(SkillCategory).where(SkillCategory.id == category_id).limit(1)
        )
        return result.scalars().first()

    async def list_categories(self, db: AsyncSession) -> list[dict[str, Any]]:
        cache_key = f"{self.CACHE_PREFIX}:categories"
        cached = await self._get_cached_json(cache_key)
        if cached is not None:
            return cached
        result = await db.execute(
            select(SkillCategory)
            .where(SkillCategory.is_enabled.is_(True))
            .order_by(SkillCategory.sort_order.asc(), SkillCategory.name.asc())
        )
        categories = [
            {
                "id": item.id,
                "slug": item.slug,
                "name": item.name,
                "description": item.description,
                "parentId": item.parent_id,
                "sortOrder": item.sort_order,
            }
            for item in result.scalars().all()
        ]
        await self._set_cached_json(cache_key, categories, 300)
        return categories

    async def list_manifest(
        self,
        db: AsyncSession,
        *,
        workspace_id: str | None = None,
        search: str | None = None,
        category_slug: str | None = None,
        include_disabled: bool = False,
    ) -> list[dict[str, Any]]:
        cache_key = (
            f"{self.CACHE_PREFIX}:manifest:"
            f"{workspace_id or 'global'}:{category_slug or 'all'}:"
            f"{include_disabled}:{search or ''}"
        )
        cached = await self._get_cached_json(cache_key)
        if cached is not None:
            return cached
        query = select(SkillRegistryEntry).order_by(SkillRegistryEntry.canonical_name.asc())
        if workspace_id:
            query = query.where(
                or_(
                    SkillRegistryEntry.workspace_id.is_(None),
                    SkillRegistryEntry.workspace_id == workspace_id,
                )
            )
        else:
            query = query.where(SkillRegistryEntry.workspace_id.is_(None))
        if not include_disabled:
            query = query.where(
                SkillRegistryEntry.is_enabled.is_(True),
                SkillRegistryEntry.status.in_(["active", "deprecated"]),
            )
        if search:
            pattern = f"%{search.strip()}%"
            query = query.where(
                or_(
                    SkillRegistryEntry.canonical_name.ilike(pattern),
                    SkillRegistryEntry.title.ilike(pattern),
                    SkillRegistryEntry.description.ilike(pattern),
                )
            )
        category_map = {
            item["id"]: item for item in await self.list_categories(db)
        }
        result = await db.execute(query)
        entries = result.scalars().all()
        output: list[dict[str, Any]] = []
        for entry in entries:
            category = category_map.get(entry.category_id)
            if category_slug and (not category or category.get("slug") != category_slug):
                continue
            output.append(self.serialize_registry_entry(entry, category))
        await self._set_cached_json(cache_key, output, 120)
        return output

    async def build_runtime_registry(
        self,
        db: AsyncSession,
        *,
        workspace_id: str | None = None,
    ) -> SkillRegistry:
        registry = build_default_registry()
        query = select(SkillRegistryEntry).where(
            SkillRegistryEntry.is_enabled.is_(True),
            SkillRegistryEntry.status == "active",
        )
        if workspace_id:
            query = query.where(
                or_(
                    SkillRegistryEntry.workspace_id.is_(None),
                    SkillRegistryEntry.workspace_id == workspace_id,
                )
            )
        else:
            query = query.where(SkillRegistryEntry.workspace_id.is_(None))
        result = await db.execute(query)
        for entry in result.scalars().all():
            if entry.source_type == "builtin":
                continue
            definition = await self._definition_from_entry(entry)
            if definition:
                registry.register(definition)
        return registry

    async def _definition_from_entry(
        self,
        entry: SkillRegistryEntry,
    ) -> SkillDefinition | None:
        try:
            if entry.runtime_kind == "python_module":
                return self._load_python_module_definition(entry)
            if entry.runtime_kind in {"http_proxy", "mcp", "openai_tool"}:
                return self._build_proxy_definition(entry)
        except Exception:
            return None
        return None

    def _load_python_module_definition(self, entry: SkillRegistryEntry) -> SkillDefinition:
        module_path = entry.module_path or entry.entrypoint
        if not module_path:
            raise ValueError("Python module skill requires module_path or entrypoint")
        attribute_name = entry.handler_name or "build_skill_definition"
        module = importlib.import_module(module_path)
        target = getattr(module, attribute_name)
        definition = target() if callable(target) else target
        if not isinstance(definition, SkillDefinition):
            raise TypeError("Dynamic skill loader must return SkillDefinition")
        return definition

    def _build_proxy_definition(self, entry: SkillRegistryEntry) -> SkillDefinition:
        manifest = SkillManifestEntry.model_validate(
            {
                "metadata": (entry.manifest_json or {}).get("metadata") or {},
                "input_schema": entry.input_schema or {},
                "output_schema": entry.output_schema or {},
            }
        )
        endpoint = entry.entrypoint or (entry.transport_config or {}).get("url")
        if not endpoint:
            raise ValueError("Proxy skill requires entrypoint or transportConfig.url")

        async def handler(context, data: GenericSkillPayload) -> dict[str, Any]:
            payload = {
                "skill_name": entry.canonical_name,
                "version": entry.latest_version,
                "input": data.model_dump(mode="json"),
                "context": context.context.model_dump(mode="json"),
                "transport": entry.transport_config or {},
            }
            try:
                response = await asyncio.to_thread(_post_json, endpoint, payload)
            except (urllib_error.HTTPError, urllib_error.URLError, TimeoutError) as exc:
                raise RuntimeError(str(exc)) from exc
            if isinstance(response, dict) and isinstance(response.get("output"), dict):
                return response["output"]
            if isinstance(response, dict):
                return response
            return {"result": response}

        return SkillDefinition(
            metadata=manifest.metadata,
            input_model=GenericSkillPayload,
            output_model=GenericSkillPayload,
            handler=handler,
        )

    def _build_openai_tool_schema(self, manifest: SkillManifestEntry) -> dict[str, Any]:
        from app.tool_adapter import build_tool_schema_bundle_from_manifest
        return build_tool_schema_bundle_from_manifest(manifest).openai_tool_schema

    def _build_mcp_tool_schema(self, manifest: SkillManifestEntry) -> dict[str, Any]:
        from app.tool_adapter import build_tool_schema_bundle_from_manifest
        return build_tool_schema_bundle_from_manifest(manifest).mcp_tool_schema

    def serialize_registry_entry(
        self,
        entry: SkillRegistryEntry,
        category: SkillCategory | dict[str, Any] | None,
    ) -> dict[str, Any]:
        if isinstance(category, SkillCategory):
            category_data = {
                "id": category.id,
                "slug": category.slug,
                "name": category.name,
                "description": category.description,
                "parentId": category.parent_id,
                "sortOrder": category.sort_order,
            }
        else:
            category_data = category
        manifest = SkillManifestEntry.model_validate(
            {
                "metadata": (entry.manifest_json or {}).get("metadata") or {},
                "input_schema": entry.input_schema or {},
                "output_schema": entry.output_schema or {},
            }
        )
        return {
            "id": entry.id,
            "workspaceId": entry.workspace_id,
            "category": category_data,
            "metadata": manifest.metadata.model_dump(mode="json"),
            "inputSchema": manifest.input_schema,
            "outputSchema": manifest.output_schema,
            "visibility": entry.visibility,
            "sourceType": entry.source_type,
            "runtimeKind": entry.runtime_kind,
            "entrypoint": entry.entrypoint,
            "modulePath": entry.module_path,
            "handlerName": entry.handler_name,
            "icon": entry.icon,
            "latestVersion": entry.latest_version,
            "status": entry.status,
            "isEnabled": entry.is_enabled,
            "openaiToolSchema": entry.openai_tool_schema,
            "mcpToolSchema": entry.mcp_tool_schema,
            "transportConfig": entry.transport_config or {},
            "cacheTtlSeconds": entry.cache_ttl_seconds,
            "embeddingStatus": entry.embedding_status,
            "embeddingText": entry.embedding_text,
            "startedAt": entry.started_at.isoformat() if entry.started_at else None,
            "stoppedAt": entry.stopped_at.isoformat() if entry.stopped_at else None,
            "createdAt": entry.created_at.isoformat() if entry.created_at else None,
            "updatedAt": entry.updated_at.isoformat() if entry.updated_at else None,
        }


skill_registry_service = SkillRegistryService()
