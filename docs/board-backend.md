# Kanban backend contract

QTable Kanban remains a view over normal table records. Business state stays in `TableRecord.data`; manual visual order is stored separately in `board_card_orders` per `(table, view, record)`.

## View config

`updateBoardViewConfig` validates and stores:

- `groupFieldId`: `select` or `text`;
- `laneFieldId`: optional `select` or single-select `member`;
- `cardFieldIds`: up to 12 existing fields;
- `hideCompleted`;
- `collapsedColumns`;
- manual card ordering.

The legacy `groupConfig.fieldId` is mirrored for compatibility.

## Query

`boardView` returns:

- columns and permission-filtered counts;
- optional swimlanes and cell counts;
- one independently paged column/lane cell;
- opaque cursor pagination;
- record and order revisions used for optimistic concurrency.

Normal select/text/number filtering and sortable field types stay database-paged. Unsupported compatibility filters/sorts are allowed only while the candidate set remains bounded (5,000 rows); larger queries fail explicitly instead of materializing an unbounded table in application memory.

## Manual order

Manual ordering uses a high-precision fractional rank. Most moves update one `board_card_orders` row. A target cell is rebalanced only if adjacent ranks are exhausted, so normal drag-and-drop does not rewrite the entire column.

`moveBoardCard` accepts optional `expectedRecordVersion` and `expectedOrderRevision`. Cross-column or cross-lane moves update the record and order metadata in one transaction and write normal record changes to ChangeSet with source `kanban`.

## Swimlanes

- select lanes use option IDs as stable keys;
- member lanes require `property.multiple=false` to keep each card in exactly one deterministic lane;
- empty values use the stable `__unassigned__` lane.

## Permissions and realtime

Counts and pages apply table/row permission rules before aggregation. Restricted member-field policies are compiled into the database query, so counts do not expose hidden records.

`boardUpdates` publishes only the affected view/card metadata. Record access is re-checked for every event; hidden/deleted card events are dropped for that subscriber.

The existing `tableUpdates` invalidation is also emitted for compatibility with current clients.
