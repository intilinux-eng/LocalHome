import os

import pytest

from localhome.util.jsonfile import load_json, save_json_atomic


def test_round_trips_through_save_and_load(tmp_path):
    path = str(tmp_path / "data.json")
    save_json_atomic(path, {"a": 1, "b": [1, 2, 3]})

    assert load_json(path) == {"a": 1, "b": [1, 2, 3]}


def test_creates_missing_parent_directories(tmp_path):
    path = str(tmp_path / "nested" / "dir" / "data.json")
    save_json_atomic(path, {"ok": True})

    assert load_json(path) == {"ok": True}


def test_overwriting_replaces_the_previous_content(tmp_path):
    path = str(tmp_path / "data.json")
    save_json_atomic(path, {"version": 1})
    save_json_atomic(path, {"version": 2})

    assert load_json(path) == {"version": 2}


def test_leaves_no_temp_file_behind_after_a_successful_write(tmp_path):
    save_json_atomic(str(tmp_path / "data.json"), {"ok": True})

    leftovers = [p for p in os.listdir(tmp_path) if p != "data.json"]
    assert leftovers == []


def test_a_failed_write_does_not_corrupt_the_existing_file(tmp_path):
    path = str(tmp_path / "data.json")
    save_json_atomic(path, {"good": "data"})

    class Unserializable:
        pass

    with pytest.raises(TypeError):
        save_json_atomic(path, {"bad": Unserializable()})

    # The temp file that failed to serialize is cleaned up, and - the
    # actual point of atomic writes - the original file was never
    # touched, since the failure happened before the rename onto it.
    assert load_json(path) == {"good": "data"}
    leftovers = [p for p in os.listdir(tmp_path) if p != "data.json"]
    assert leftovers == []
