"""
迁移脚本：为 ai_messages 表添加 table_ids 列
运行: python scripts/migrate_add_table_ids_to_ai_messages.py
"""
import sys
import os

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text
from app.core.config import settings


def migrate():
    """为 ai_messages 表添加 table_ids 列"""
    database_uri = settings.SQLALCHEMY_DATABASE_URI
    if not database_uri:
        print("错误: 未配置数据库 URI")
        return False

    engine = create_engine(database_uri, echo=True)
    
    with engine.connect() as conn:
        # 检查列是否已存在
        if "sqlite" in database_uri:
            result = conn.execute(text("PRAGMA table_info(ai_messages)"))
            columns = [row[1] for row in result]
            if "table_ids" in columns:
                print("table_ids 列已存在，跳过迁移")
                return True
            
            # SQLite 添加列
            conn.execute(text("ALTER TABLE ai_messages ADD COLUMN table_ids TEXT"))
        else:
            # PostgreSQL 或其他数据库
            result = conn.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'ai_messages' AND column_name = 'table_ids'"
            ))
            if result.first():
                print("table_ids 列已存在，跳过迁移")
                return True
            
            # 添加列
            conn.execute(text("ALTER TABLE ai_messages ADD COLUMN table_ids TEXT"))
        
        conn.commit()
        print("成功: 已为 ai_messages 表添加 table_ids 列")
        return True


if __name__ == "__main__":
    print("开始迁移...")
    if migrate():
        print("迁移完成")
    else:
        print("迁移失败")
        sys.exit(1)
