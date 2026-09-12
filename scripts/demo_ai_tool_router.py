import asyncio
import json
import os

import httpx


async def main() -> None:
    base_url = os.getenv("QTABLE_BASE_URL", "http://localhost:8000")
    token = os.getenv("QTABLE_TOKEN")
    if not token:
        raise RuntimeError("Please set QTABLE_TOKEN before running this demo.")

    payload = {
        "message": "帮我查看延期任务，并按负责人统计数量",
        "workspaceId": os.getenv("QTABLE_WORKSPACE_ID", "wkbDefault"),
        "tableIds": [os.getenv("QTABLE_TABLE_ID", "dstDefault")],
        "allowedSkills": [
            "qtable.record.query",
            "qtable.table.describe",
        ],
        "toolContext": {
            "sessionId": "demo-session",
            "tableIds": [os.getenv("QTABLE_TABLE_ID", "dstDefault")],
            "variables": {},
            "toolResults": [],
            "notes": ["This is a demo request for multi-tool routing."],
            "chainDepth": 0,
            "retryPolicy": {
                "maxAttempts": 2,
                "backoffMs": 350,
            },
        },
        "maxSteps": 6,
        "toolLimit": 8,
        "confirmed": False,
        "locale": "zh-CN",
        "timezone": "Asia/Shanghai",
    }

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            f"{base_url}/api/ai/router/run",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "X-Workspace-Id": os.getenv("QTABLE_WORKSPACE_ID", "wkbDefault"),
            },
            json=payload,
        )
        response.raise_for_status()
        print(json.dumps(response.json(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
