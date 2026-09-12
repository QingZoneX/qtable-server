# My Work aggregate API

My Work is the server-side workbench contract used by QTable home/workspace surfaces. It aggregates task/project semantics without asking the browser to download every table.

## GraphQL

```graphql
query MyWork(
  $sections: [String!]
  $limit: Int!
  $cursors: JSON
  $timezone: String!
  $taskState: String!
) {
  myWork(
    sections: $sections
    limit: $limit
    cursors: $cursors
    timezone: $timezone
    taskState: $taskState
  )
}
```

Supported sections are `tasks`, `due`, `projects`, `recent`, `activity`, and `kpi`. The default request returns all sections. A section failure is isolated and returned as `status: "error"`; it does not blank the entire workbench.

The response includes:

- `generatedAt` in UTC;
- the requested IANA timezone;
- per-section data/status;
- cursor page information;
- per-section and total server timing in milliseconds;
- cache mode/reason.

`taskState` supports `all`, `in_progress`, `not_started`, and `completed`.

## Business semantics

Tasks, due windows, project progress, and KPIs consume the saved Table Task Profile. Runtime code never guesses fields by display name.

The status contract includes three explicit sets:

- `completedStatusValues`;
- `blockedStatusValues`;
- `notStartedStatusValues`.

Historical migration may suggest these values from labels, but the suggestion must be confirmed and persisted before My Work consumes it. The project-management system template defines all three explicitly.

Tables without a Task Profile remain normal multidimensional tables. They are excluded from task/project aggregation but can still contribute permission-safe generic activity.

## Timezone and due dates

The client sends an IANA timezone such as `Asia/Shanghai` or `UTC`.

Date-only values are due at the end of that local day. Timestamp values are interpreted as ISO-8601 instants. Due sections are calculated in SQL and return exact counts for:

- overdue;
- today;
- next 24 hours;
- next 3 days;
- next 7 days.

Completed tasks are excluded from due/risk windows. A not-started task can still be due today or soon.

## Permission boundary

My Work is intentionally permission-first.

Every task/project source requires current table read permission. Row-level policies (`all`, `creator`, `member_field`) are applied before task lists, project counts, overdue counts, KPIs, and activity are returned. Manager semantics match the existing row-permission service.

Recent targets are re-resolved on every read instead of trusting persisted titles or links. A deleted target or a target the user can no longer read disappears immediately.

Activity also checks the current record before exposing history. Activity for a deleted/inaccessible row is omitted, so old ChangeSet data cannot be used to probe a record after access is revoked.

## Recent targets and local migration

Recent navigation is stored server-side in `my_work_recent_targets` and therefore follows the user across devices.

Mutations:

```graphql
mutation UpsertRecent($target: JSON!) {
  upsertRecentTarget(target: $target)
}

mutation ImportRecent($targets: [JSON!]!) {
  importRecentTargets(targets: $targets)
}

mutation RemoveRecent($entityType: String!, $entityId: String!, $tableId: String) {
  removeRecentTarget(
    entityType: $entityType
    entityId: $entityId
    tableId: $tableId
  )
}
```

`importRecentTargets` accepts the existing QTableUI `qtable.recentTargets.v1.*` shape, including nested `table.id/defaultViewId` and millisecond `visitedAt`. Local `title`, `subtitle`, and `deepLink` are treated as untrusted hints: the backend resolves the current target and builds canonical metadata.

Canonical links are:

- table: `/workbench/{tableId}/{viewId}`;
- dashboard: `/workbench/{dashboardId}`;
- record: `/workbench/{tableId}/{viewId}?recordId={recordId}`.

## Activity

Activity is backed by ChangeSet/ChangeItem. v1 classifies:

- `record.created`;
- `record.updated`;
- `task.status_changed`;
- `task.assignee_changed`;
- comment activity when a comment ChangeItem is introduced by the collaboration/comment feature.

The feed includes time, actor, entity/table/workspace metadata, operation/summary, and a deep link.

## Pagination

Each paged section accepts an opaque cursor through the top-level `cursors` object, for example:

```json
{
  "tasks": "<cursor>",
  "activity": "<cursor>"
}
```

Cursors are opaque to clients. v1 uses a bounded offset cursor to keep the public contract stable while allowing a later keyset implementation.

## Performance strategy

My Work does not download a 100k-row project into Python or the browser.

- task rows are filtered by table permission, row scope, assignee, and task state in SQL, then only the required global page window is materialized;
- due buckets are filtered and counted in SQL;
- project totals/completed/progress/overdue/blocked values use SQL aggregate functions;
- activity scans a bounded recent ChangeSet window;
- recent targets are capped per user.

QTable currently stores dynamic record values in the generic JSON column. `table_records(table_id, created_by_user_id)` already supports the principal row-scope path. PostgreSQL queries cast dynamic data to JSONB so array containment can use a future GIN/expression strategy.

A single fixed expression index cannot efficiently index arbitrary Task Profile field IDs for status/due/assignee at the same time. For that reason this PR does not add unsafe template-specific indexes to the generic table. Production telemetry should use the returned `performance.sectionMs` and `performance.totalMs` to establish P50/P95 first. If a deployment shows large project scans dominating P95, the next database optimization should be profile-aware generated/index tables or a maintained projection, rather than hard-coding field names.

## Cache policy

v1 deliberately uses no cross-request aggregate cache. Row visibility may depend on mutable JSON member fields and permissions can change independently of table data. A short TTL could therefore leak a recently revoked row.

The response reports `cache.mode = "none"`. A future cache is safe only when its key/invalidation boundary includes user identity, table/item permission version, row-policy version, and row-scope/data version.

## Compatibility

The query supports PostgreSQL and SQLite. PostgreSQL is the standard production/development database; SQLite remains the explicit lightweight fallback. Both implement the same permission and aggregation semantics, while PostgreSQL is the target for large-project performance.
