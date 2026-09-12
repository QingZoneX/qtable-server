import asyncio

from app.db.init_db import init_models
from app.db.session import AsyncSessionLocal
from app.services.skill_registry import (
    SkillRegistrationRequest,
    skill_registry_service,
)


async def main() -> None:
    await init_models()
    async with AsyncSessionLocal() as session:
        registered = await skill_registry_service.register_skill(
            session,
            SkillRegistrationRequest.model_validate(
                {
                    "name": "qtable.note.summarize",
                    "version": "1.0.0",
                    "title": "Summarize Note",
                    "description": "Generate a concise summary for a qtable note.",
                    "category": "Notes",
                    "workspaceId": "wkbDefault",
                    "tags": ["notes", "summary", "ai"],
                    "visibility": "workspace",
                    "source_type": "plugin",
                    "runtime_kind": "http_proxy",
                    "side_effect": "none",
                    "confirmation_required": False,
                    "idempotent": True,
                    "supports_dry_run": True,
                    "entrypoint": "http://localhost:8010/skills/note-summarize",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "noteId": {"type": "string"},
                            "maxLength": {"type": "integer", "default": 200},
                        },
                        "required": ["noteId"],
                        "additionalProperties": False,
                    },
                    "outputSchema": {
                        "type": "object",
                        "properties": {
                            "summary": {"type": "string"},
                            "keywords": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "required": ["summary"],
                        "additionalProperties": False,
                    },
                    "permissions": [
                        {
                            "resource": "workspace",
                            "action": "read",
                            "target_param": None,
                            "optional": True,
                        }
                    ],
                    "transportConfig": {
                        "protocol": "http",
                        "timeoutMs": 15000,
                    },
                    "embeddingText": "Summarize qtable notes into short AI-friendly summaries.",
                    "changelog": "Initial example skill registration",
                }
            ),
            actor="system-script",
        )
        print(registered)


if __name__ == "__main__":
    asyncio.run(main())
