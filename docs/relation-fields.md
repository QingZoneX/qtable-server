# SmartTable 关联记录字段

## 目标

`relation` 字段用于把当前表的一条记录关联到同一 workspace 中另一张表（也允许自关联）的记录。

关联字段只保存目标记录 ID，不保存目标记录标题或完整快照。目标记录名称发生变化后，选择器和展示层通过目标表实时解析，因此不需要回写所有源记录。

## 字段契约

```json
{
  "id": "f_project",
  "name": "关联项目",
  "type": "relation",
  "property": {
    "targetTableId": "dst_projects",
    "displayFieldId": "f_name",
    "multiple": true
  }
}
```

- `targetTableId`：必填，目标表 ID；DB 模式必须与源表位于同一 workspace。
- `displayFieldId`：可选，目标表中用于显示标题的字段；未指定时默认使用目标表第一个字段。
- `multiple`：`true` 为多关联，`false` 为单关联。

## 记录值

### 多关联

```json
["r1001", "r1002"]
```

服务端会去重并保持用户选择顺序，单字段最多关联 200 条记录。

### 单关联

```json
"r1001"
```

空值为 `null`。服务端拒绝向单关联字段写入多个记录 ID。

## 服务端校验

所有写入口统一经过 `smart_table_store`：

- 普通单元格更新；
- 多字段更新；
- 批量插入；
- 协作 record patches；
- AI/自动化复用的 Store 写入口。

校验包括：

1. 目标表存在；
2. DB 模式下源表和目标表位于同一 workspace；
3. `displayFieldId` 属于目标表；
4. 被写入的目标 recordId 实际存在于目标表；
5. 单/多关联值形态符合字段配置；
6. 多关联自动去重，最多 200 条。

## 候选记录查询

GraphQL 新增：

```graphql
relationOptions(
  tableId: String!
  fieldId: String!
  search: String
  offset: Int = 0
  limit: Int = 50
  recordIds: [String!]
): JSON!
```

普通搜索/分页返回示例：

```json
{
  "targetTableId": "dst_projects",
  "displayFieldId": "f_name",
  "multiple": true,
  "total": 120,
  "hasMore": true,
  "items": [
    {"id": "r1001", "title": "项目 A"}
  ]
}
```

当传入 `recordIds` 时，接口忽略分页和搜索条件，按传入顺序精确解析已保存关联 ID 的标题；单次最多解析 200 个 ID，前端应按 200 条分批。该能力用于保证已选记录不受候选列表分页影响。

DB 模式下查询源表和目标表都会执行 read 权限检查。

## 删除一致性

删除目标表中的记录时，服务端会在同一个数据库会话中扫描当前 workspace 内指向该目标表的 relation 字段，并从源记录中移除对应 recordId，避免产生悬空引用。

当前文件后端没有 workspace 级反向索引，因此只提供写入校验，不做跨文件表的全局反向清理；生产 DB 模式提供完整清理语义。

## 后续扩展

该契约为后续能力预留基础：

- 双向关联/backlink；
- Lookup；
- Rollup；
- 关联记录计数与聚合；
- 服务端关系筛选和索引优化。
