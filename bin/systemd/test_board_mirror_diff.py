#!/usr/bin/env python3
"""assert-based checks for board_mirror_diff, the incremental-write/delete transform behind
run-board-mirror.sh."""

from board_mirror_diff import diff_writes, snapshot_from_writes

META = {"op": "set", "collection": "meta", "doc_id": "status", "data": {"written_at": 2}}


def _set(collection, doc_id, data):
    return {"op": "set", "collection": collection, "doc_id": doc_id, "data": data}


def test_first_run_with_no_previous_sends_everything():
    writes = [_set("sessions", "a", {"state": "running"}), META]

    assert diff_writes({}, writes) == writes


def test_unchanged_doc_is_dropped_but_meta_status_always_kept():
    writes = [_set("sessions", "a", {"state": "running"}), META]
    previous = snapshot_from_writes(writes)

    result = diff_writes(previous, writes)

    assert result == [META]


def test_changed_doc_is_sent_new_doc_is_sent():
    previous = snapshot_from_writes([_set("sessions", "a", {"state": "running"})])
    writes = [
        _set("sessions", "a", {"state": "waiting"}),  # changed
        _set("sessions", "b", {"state": "running"}),  # new
        META,
    ]

    result = diff_writes(previous, writes)

    assert result == [writes[0], writes[1], META]


def test_doc_missing_from_this_run_becomes_a_delete_before_meta_status():
    previous = snapshot_from_writes(
        [_set("sessions", "a", {"state": "running"}), _set("tickets", "7763", {"state": "done"})]
    )
    # "tickets/7763" no longer exists this run — e.g. handed off to QC and closed out.
    writes = [_set("sessions", "a", {"state": "running"}), META]

    result = diff_writes(previous, writes)

    assert result == [
        {"op": "delete", "collection": "tickets", "doc_id": "7763"},
        META,
    ]


def test_snapshot_from_writes_drops_meta_status_key_shape_and_keeps_only_data():
    writes = [_set("sessions", "a", {"state": "running"}), META]

    assert snapshot_from_writes(writes) == {
        "sessions/a": {"state": "running"},
        "meta/status": {"written_at": 2},
    }
