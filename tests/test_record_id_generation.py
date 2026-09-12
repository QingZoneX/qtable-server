from app.services.smart_table_store.db_backend import _new_record_id


def test_record_ids_are_unique_across_rapid_batches() -> None:
    first_batch = [_new_record_id() for _ in range(250)]
    second_batch = [_new_record_id() for _ in range(250)]

    all_ids = first_batch + second_batch
    assert len(all_ids) == 500
    assert len(set(all_ids)) == 500
    assert all(record_id.startswith("r") for record_id in all_ids)


def test_record_id_format_is_uuid_hex() -> None:
    record_id = _new_record_id()

    assert len(record_id) == 33
    assert record_id[0] == "r"
    int(record_id[1:], 16)
