from sqlalchemy import Column, String, Integer, JSON
from app.db.base import Base

class TableField(Base):
    __tablename__ = "table_fields"
    
    id = Column(String, primary_key=True)
    table_id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    type = Column(String, nullable=False)
    options = Column(JSON, nullable=True)
    property = Column(JSON, nullable=True)
    order_index = Column(Integer, default=0)

class TableRecord(Base):
    __tablename__ = "table_records"
    
    id = Column(String, primary_key=True)
    table_id = Column(String, primary_key=True)
    data = Column(JSON, nullable=False)
    order_index = Column(Integer, default=0)
    created_by_user_id = Column(Integer, nullable=True)
    # Monotonic optimistic-concurrency version. Existing databases are
    # auto-repaired to version=1 by init_db._sync_ensure_schema.
    version = Column(Integer, nullable=False, default=1)

class TableView(Base):
    __tablename__ = "table_views"
    
    id = Column(String, primary_key=True)
    table_id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    type = Column(String, nullable=False)
    config = Column(JSON, nullable=True)

class TableFilter(Base):
    __tablename__ = "table_filters"
    
    id = Column(String, primary_key=True)
    table_id = Column(String, primary_key=True)
    field_id = Column(String, nullable=False)
    operator = Column(String, nullable=False)
    value = Column(JSON, nullable=True)
    logic = Column(String, nullable=False, default="and")
    order_index = Column(Integer, default=0)

class TableSort(Base):
    __tablename__ = "table_sorts"
    
    id = Column(String, primary_key=True)
    table_id = Column(String, primary_key=True)
    field_id = Column(String, nullable=False)
    order = Column(String, nullable=False)  # 'asc' or 'desc'
    order_index = Column(Integer, default=0)

class TableGroup(Base):
    __tablename__ = "table_groups"
    
    id = Column(String, primary_key=True)
    table_id = Column(String, primary_key=True)
    field_id = Column(String, nullable=True)
    order = Column(String, nullable=False, default="asc")

class TableRowPermissionPolicy(Base):
    __tablename__ = "table_row_permission_policies"

    table_id = Column(String, primary_key=True)
    mode = Column(String, nullable=False, default="all")
    member_field_id = Column(String, nullable=True)


class WorkspaceItem(Base):
    __tablename__ = "workspace_items"

    id = Column(String, primary_key=True)
    workspace_id = Column(String, nullable=False, index=True)
    type = Column(String, nullable=False)
    name = Column(String, nullable=False)
    parent_id = Column(String, nullable=True, index=True)
    order_index = Column(Integer, default=0)
    default_view_id = Column(String, nullable=True)

class WorkspaceItemPermission(Base):
    __tablename__ = "workspace_item_permissions"

    item_id = Column(String, primary_key=True)
    user_id = Column(Integer, primary_key=True)
    permission = Column(String, nullable=False, default="read")
