import strawberry
import uuid
from typing import Optional, List
from strawberry.types import Info
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from app.db.session import AsyncSessionLocal
from app.models.ai_config import AiConfig as AiConfigModel
from app.models.ai_conversation import AiConversation as AiConversationModel
from app.models.ai_message import AiMessage as AiMessageModel
from app.services.encryption import encrypt_api_key, decrypt_api_key


@strawberry.type(name="AiConfig")
class AiConfigType:
    id: strawberry.ID
    provider: str
    model: str
    createdAt: str
    updatedAt: Optional[str] = None


@strawberry.type(name="AiMessage")
class AiMessageType:
    id: strawberry.ID
    role: str
    content: str
    tableIds: Optional[List[str]] = None  # 此消息关联的数据表ID列表
    createdAt: str


@strawberry.type(name="AiConversation")
class AiConversationType:
    id: strawberry.ID
    tableId: Optional[str] = None  # 保留向后兼容
    tableIds: Optional[List[str]] = None  # 支持多个表
    title: str
    messages: List[AiMessageType]
    createdAt: str
    updatedAt: Optional[str] = None


@strawberry.input(name="AiConfigInput")
class AiConfigInput:
    id: Optional[str] = None  # 如果提供则更新现有配置，否则创建新配置
    provider: str
    apiKey: str
    model: Optional[str] = None


@strawberry.type
class AiQuery:
    @strawberry.field(name="aiConfigs")
    async def ai_configs(self, info: Info) -> List[AiConfigType]:
        """获取当前用户的所有AI配置"""
        from app.core.security import decode_access_token
        request = info.context.get("request") or info.context.get("websocket")
        if request:
            auth_header = request.headers.get("Authorization", "")
            if auth_header.lower().startswith("bearer "):
                token = auth_header.split(" ", 1)[1]
                try:
                    payload = decode_access_token(token)
                    user_id = payload.get("user_id")
                    if user_id:
                        async with AsyncSessionLocal() as session:
                            result = await session.execute(
                                select(AiConfigModel).where(
                                    AiConfigModel.user_id == str(user_id)
                                ).order_by(AiConfigModel.created_at.desc())
                            )
                            configs = result.scalars().all()
                            return [
                                AiConfigType(
                                    id=config.id,
                                    provider=config.provider,
                                    model=config.model,
                                    createdAt=config.created_at.isoformat() if config.created_at else "",
                                    updatedAt=config.updated_at.isoformat() if config.updated_at else None,
                                )
                                for config in configs
                            ]
                except Exception:
                    pass
        return []

    @strawberry.field(name="aiConfig")
    async def ai_config(self, info: Info) -> Optional[AiConfigType]:
        """获取当前用户的默认AI配置（兼容旧接口）"""
        from app.core.security import decode_access_token
        request = info.context.get("request") or info.context.get("websocket")
        if request:
            auth_header = request.headers.get("Authorization", "")
            if auth_header.lower().startswith("bearer "):
                token = auth_header.split(" ", 1)[1]
                try:
                    payload = decode_access_token(token)
                    user_id = payload.get("user_id")
                    if user_id:
                        async with AsyncSessionLocal() as session:
                            result = await session.execute(
                                select(AiConfigModel).where(
                                    AiConfigModel.user_id == str(user_id)
                                ).order_by(AiConfigModel.created_at.desc())
                            )
                            config = result.scalars().first()
                            if not config:
                                return None
                            return AiConfigType(
                                id=config.id,
                                provider=config.provider,
                                model=config.model,
                                createdAt=config.created_at.isoformat() if config.created_at else "",
                                updatedAt=config.updated_at.isoformat() if config.updated_at else None,
                            )
                except Exception:
                    return None
        return None

    @strawberry.field(name="conversations")
    async def conversations(
        self, info: Info, tableId: Optional[str] = None
    ) -> List[AiConversationType]:
        from app.core.security import decode_access_token
        import json
        request = info.context.get("request") or info.context.get("websocket")
        user_id = None
        if request:
            auth_header = request.headers.get("Authorization", "")
            if auth_header.lower().startswith("bearer "):
                token = auth_header.split(" ", 1)[1]
                try:
                    payload = decode_access_token(token)
                    user_id = payload.get("user_id")
                except Exception:
                    pass
        if not user_id:
            return []
        async with AsyncSessionLocal() as session:
            query = select(AiConversationModel).where(
                AiConversationModel.user_id == str(user_id)
            )
            # 如果指定了 tableId，则筛选包含该表的对话
            if tableId:
                # 匹配 table_id 字段或 table_ids JSON 数组中包含该 ID 的记录
                query = query.where(
                    (AiConversationModel.table_id == tableId) |
                    (AiConversationModel.table_ids.like(f'%"{tableId}"%'))
                )
            query = query.order_by(AiConversationModel.updated_at.desc())
            result = await session.execute(query)
            conversations = result.scalars().all()
            output = []
            for conv in conversations:
                msgs_result = await session.execute(
                    select(AiMessageModel)
                    .where(AiMessageModel.conversation_id == conv.id)
                    .order_by(AiMessageModel.created_at.asc())
                )
                messages = msgs_result.scalars().all()
                # 解析 table_ids
                table_ids = []
                if conv.table_ids:
                    try:
                        table_ids = json.loads(conv.table_ids)
                    except Exception:
                        table_ids = []
                if not table_ids and conv.table_id:
                    table_ids = [conv.table_id]
                
                # 解析每个消息的 table_ids
                message_types = []
                for m in messages:
                    msg_table_ids = []
                    if m.table_ids:
                        try:
                            msg_table_ids = json.loads(m.table_ids)
                        except Exception:
                            msg_table_ids = []
                    message_types.append(
                        AiMessageType(
                            id=m.id,
                            role=m.role,
                            content=m.content,
                            tableIds=msg_table_ids if msg_table_ids else None,
                            createdAt=m.created_at.isoformat() if m.created_at else "",
                        )
                    )
                
                output.append(
                    AiConversationType(
                        id=conv.id,
                        tableId=conv.table_id,
                        tableIds=table_ids,
                        title=conv.title,
                        createdAt=conv.created_at.isoformat() if conv.created_at else "",
                        updatedAt=conv.updated_at.isoformat() if conv.updated_at else None,
                        messages=message_types,
                    )
                )
            return output


@strawberry.type
class AiMutation:
    @strawberry.mutation(name="saveAiConfig")
    async def save_ai_config(self, info: Info, input: AiConfigInput) -> AiConfigType:
        from app.core.security import decode_access_token
        from datetime import datetime, timezone
        request = info.context.get("request") or info.context.get("websocket")
        user_id = None
        if request:
            auth_header = request.headers.get("Authorization", "")
            if auth_header.lower().startswith("bearer "):
                token = auth_header.split(" ", 1)[1]
                try:
                    payload = decode_access_token(token)
                    user_id = payload.get("user_id")
                except Exception:
                    pass
        if not user_id:
            raise Exception("Unauthorized")
        encrypted = encrypt_api_key(input.apiKey)
        model = input.model or "deepseek-chat"
        now = datetime.now(timezone.utc)
        async with AsyncSessionLocal() as session:
            # 如果提供了 id，则更新现有配置
            if input.id:
                result = await session.execute(
                    select(AiConfigModel).where(
                        AiConfigModel.id == input.id,
                        AiConfigModel.user_id == str(user_id)
                    )
                )
                config = result.scalars().first()
                if config:
                    config.provider = input.provider
                    config.api_key_encrypted = encrypted
                    config.model = model
                    config.updated_at = now
                else:
                    # id 不存在，创建新配置
                    config = AiConfigModel(
                        id=input.id,
                        user_id=str(user_id),
                        provider=input.provider,
                        api_key_encrypted=encrypted,
                        model=model,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(config)
            else:
                # 没有提供 id，创建新配置
                config = AiConfigModel(
                    id=str(uuid.uuid4()),
                    user_id=str(user_id),
                    provider=input.provider,
                    api_key_encrypted=encrypted,
                    model=model,
                    created_at=now,
                    updated_at=now,
                )
                session.add(config)
            await session.commit()
            return AiConfigType(
                id=config.id,
                provider=config.provider,
                model=config.model,
                createdAt=config.created_at.isoformat() if config.created_at else "",
                updatedAt=config.updated_at.isoformat() if config.updated_at else None,
            )

    @strawberry.mutation(name="deleteAiConfig")
    async def delete_ai_config(self, info: Info, id: strawberry.ID) -> bool:
        from app.core.security import decode_access_token
        request = info.context.get("request") or info.context.get("websocket")
        user_id = None
        if request:
            auth_header = request.headers.get("Authorization", "")
            if auth_header.lower().startswith("bearer "):
                token = auth_header.split(" ", 1)[1]
                try:
                    payload = decode_access_token(token)
                    user_id = payload.get("user_id")
                except Exception:
                    pass
        if not user_id:
            raise Exception("Unauthorized")
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(AiConfigModel).where(
                    AiConfigModel.id == id,
                    AiConfigModel.user_id == str(user_id)
                )
            )
            config = result.scalars().first()
            if config:
                await session.delete(config)
                await session.commit()
            return True

    @strawberry.mutation(name="createConversation")
    async def create_conversation(
        self, info: Info, tableId: Optional[str] = None, tableIds: Optional[List[str]] = None, title: Optional[str] = None
    ) -> AiConversationType:
        from datetime import datetime, timezone
        import json
        request = info.context.get("request") or info.context.get("websocket")
        user_id = None
        if request:
            from app.core.security import decode_access_token
            auth_header = request.headers.get("Authorization", "")
            if auth_header.lower().startswith("bearer "):
                token = auth_header.split(" ", 1)[1]
                try:
                    payload = decode_access_token(token)
                    user_id = payload.get("user_id")
                except Exception:
                    pass
        if not user_id:
            raise Exception("Unauthorized")
        now = datetime.now(timezone.utc)
        # 处理 tableIds
        final_table_ids = tableIds or []
        if tableId and tableId not in final_table_ids:
            final_table_ids = [tableId] + final_table_ids
        table_ids_json = json.dumps(final_table_ids) if final_table_ids else None
        async with AsyncSessionLocal() as session:
            conv = AiConversationModel(
                id=str(uuid.uuid4()),
                user_id=str(user_id),
                table_id=tableId,
                table_ids=table_ids_json,
                title=title or "New Conversation",
                created_at=now,
                updated_at=now,
            )
            session.add(conv)
            await session.commit()
            return AiConversationType(
                id=conv.id,
                tableId=conv.table_id,
                tableIds=final_table_ids,
                title=conv.title,
                createdAt=conv.created_at.isoformat(),
                updatedAt=conv.updated_at.isoformat(),
                messages=[],
            )

    @strawberry.mutation(name="deleteConversation")
    async def delete_conversation(self, info: Info, id: strawberry.ID) -> bool:
        request = info.context.get("request") or info.context.get("websocket")
        user_id = None
        if request:
            from app.core.security import decode_access_token
            auth_header = request.headers.get("Authorization", "")
            if auth_header.lower().startswith("bearer "):
                token = auth_header.split(" ", 1)[1]
                try:
                    payload = decode_access_token(token)
                    user_id = payload.get("user_id")
                except Exception:
                    pass
        if not user_id:
            raise Exception("Unauthorized")
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(AiConversationModel).where(
                    AiConversationModel.id == id,
                    AiConversationModel.user_id == str(user_id),
                )
            )
            conv = result.scalars().first()
            if conv:
                await session.execute(
                    delete(AiMessageModel).where(AiMessageModel.conversation_id == id)
                )
                await session.delete(conv)
                await session.commit()
            return True

    @strawberry.mutation(name="clearConversationMessages")
    async def clear_conversation_messages(self, info: Info, id: strawberry.ID) -> bool:
        request = info.context.get("request") or info.context.get("websocket")
        user_id = None
        if request:
            from app.core.security import decode_access_token
            auth_header = request.headers.get("Authorization", "")
            if auth_header.lower().startswith("bearer "):
                token = auth_header.split(" ", 1)[1]
                try:
                    payload = decode_access_token(token)
                    user_id = payload.get("user_id")
                except Exception:
                    pass
        if not user_id:
            raise Exception("Unauthorized")
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(AiConversationModel).where(
                    AiConversationModel.id == id,
                    AiConversationModel.user_id == str(user_id),
                )
            )
            conv = result.scalars().first()
            if not conv:
                return False
            await session.execute(
                delete(AiMessageModel).where(AiMessageModel.conversation_id == id)
            )
            await session.commit()
            return True

    @strawberry.mutation(name="updateConversation")
    async def update_conversation(
        self, info: Info, id: strawberry.ID, title: str
    ) -> AiConversationType:
        """更新对话标题"""
        from datetime import datetime, timezone
        request = info.context.get("request") or info.context.get("websocket")
        user_id = None
        if request:
            from app.core.security import decode_access_token
            auth_header = request.headers.get("Authorization", "")
            if auth_header.lower().startswith("bearer "):
                token = auth_header.split(" ", 1)[1]
                try:
                    payload = decode_access_token(token)
                    user_id = payload.get("user_id")
                except Exception:
                    pass
        if not user_id:
            raise Exception("Unauthorized")
        
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(AiConversationModel).where(
                    AiConversationModel.id == id,
                    AiConversationModel.user_id == str(user_id),
                )
            )
            conv = result.scalars().first()
            if not conv:
                raise Exception("Conversation not found")
            
            conv.title = title
            conv.updated_at = datetime.now(timezone.utc)
            await session.commit()
            
            # 获取消息
            msgs_result = await session.execute(
                select(AiMessageModel)
                .where(AiMessageModel.conversation_id == conv.id)
                .order_by(AiMessageModel.created_at.asc())
            )
            messages = msgs_result.scalars().all()
            
            # 解析 table_ids
            import json
            table_ids = []
            if conv.table_ids:
                try:
                    table_ids = json.loads(conv.table_ids)
                except Exception:
                    table_ids = []
            if not table_ids and conv.table_id:
                table_ids = [conv.table_id]
            
            return AiConversationType(
                id=conv.id,
                tableId=conv.table_id,
                tableIds=table_ids,
                title=conv.title,
                createdAt=conv.created_at.isoformat() if conv.created_at else "",
                updatedAt=conv.updated_at.isoformat() if conv.updated_at else None,
                messages=[
                    AiMessageType(
                        id=m.id,
                        role=m.role,
                        content=m.content,
                        createdAt=m.created_at.isoformat() if m.created_at else "",
                    )
                    for m in messages
                ],
            )
