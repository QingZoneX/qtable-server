"""
数据库迁移脚本：为 ai_conversations 表添加 table_ids 字段
"""
import asyncio
from sqlalchemy import text
from app.db.session import engine


async def migrate():
    async with engine.begin() as conn:
        # 检查 table_ids 字段是否已存在
        check_sql = text("""
            SELECT column_name 
            FROM information_schema.columns 
            WHERE table_name = 'ai_conversations' 
            AND column_name = 'table_ids'
        """)
        result = await conn.execute(check_sql)
        exists = result.first() is not None
        
        if not exists:
            # 添加 table_ids 字段
            alter_sql = text("""
                ALTER TABLE ai_conversations 
                ADD COLUMN table_ids TEXT
            """)
            await conn.execute(alter_sql)
            print("✓ 成功添加 table_ids 字段到 ai_conversations 表")
        else:
            print("! table_ids 字段已存在，跳过迁移")


if __name__ == "__main__":
    asyncio.run(migrate())
