"""Offline exports expose a fixed numeric/boolean schema, never content."""
import json
import socket
import sqlite3
from unittest.mock import Mock

import pytest

from gcl.cli import _parser, run
from gcl.core.store import LocalStore
from gcl.observe import export_beta_summary


def populate(store):
    with store._tx() as db:
        for mid in ("private-mission", "secret-token"):
            db.execute("INSERT INTO missions(id, version, mode) VALUES (?,1,'OBSERVE_ONLY')", (mid,))
        for mid, kind, at in [
            ("private-mission", "mission.created", "2026-10-20 23:30:00-02:00"),
            ("private-mission", "mission.completed", "2026-10-21 12:00:00"),
            ("private-mission", "mission.completed", "2026-10-21 13:00:00"),
            ("secret-token", "mission.succeeded", "2026-10-22 00:00:00"),
        ]:
            db.execute("INSERT INTO events(mission_id,type,data,created_at) VALUES (?,?,?,?)",
                       (mid, kind, json.dumps({"prompt": "TOP SECRET PROMPT", "path": "/Users/private/repo",
                                              "api_key": "sk-secret-token"}), at))
        db.execute("INSERT INTO rollovers(id,mission_id,state,epoch,reason) "
                   "VALUES ('private-rollover','private-mission','ABORTED',1,'secret reason')")


def test_populated_summary_privacy_and_offline(tmp_path, monkeypatch):
    store = LocalStore(tmp_path / "state.sqlite")
    populate(store)
    blocked = Mock(side_effect=AssertionError("network forbidden"))
    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    summary = export_beta_summary(store, 12, 3, True)
    assert summary == {
        "schema_version": "gcl.beta_summary.v1", "missions_total": 2, "missions_completed": 2,
        "rollovers_total": 1, "active_days_count": 2, "repeat_use_qualified": True,
        "support_minutes": 12, "false_positives": 3, "key_takeaway_present": True,
        "privacy_mode": "STATS_ONLY_NO_CONTENT_NO_PATHS",
    }
    assert summary == export_beta_summary(store, 12, 3, True)
    encoded = json.dumps(summary)
    for private in (str(tmp_path), "/Users", "private", "secret", "PROMPT", "api_key", "reason"):
        assert private not in encoded
    blocked.assert_not_called()
    cli_summary = run(_parser().parse_args(["--home", str(tmp_path), "beta-summary",
                                          "--support-minutes", "12", "--false-positives", "3",
                                          "--key-takeaway-present"]))
    assert cli_summary == summary


def test_empty_store(tmp_path):
    store = LocalStore(tmp_path / "state.sqlite")
    summary = export_beta_summary(store)
    assert summary["missions_total"] == summary["missions_completed"] == summary["rollovers_total"] == 0
    assert summary["active_days_count"] == 0
    assert summary["repeat_use_qualified"] is False
    assert summary["support_minutes"] == summary["false_positives"] == 0
    assert summary["key_takeaway_present"] is False


@pytest.mark.parametrize("missions,dates,expected", [(1, ["2026-10-20", "2026-10-21"], True),
                                                      (2, ["2026-10-20"], True),
                                                      (1, ["2026-10-20"], False)])
def test_repeat_use_alternatives(tmp_path, missions, dates, expected):
    store = LocalStore(tmp_path / "state.sqlite")
    with store._tx() as db:
        for i in range(missions):
            db.execute("INSERT INTO missions(id,version,mode) VALUES (?,1,'OFF')", (str(i),))
        for date in dates:
            db.execute("INSERT INTO events(mission_id,type,data,created_at) VALUES ('0','activity','{}',?)", (date,))
    assert export_beta_summary(store)["repeat_use_qualified"] is expected


@pytest.mark.parametrize("kwargs", [{"support_minutes": "secret"}, {"false_positives": -1},
                                    {"support_minutes": True}, {"key_takeaway_present": "secret"}])
def test_voluntary_metadata_cannot_smuggle_content(tmp_path, kwargs):
    with pytest.raises(ValueError):
        export_beta_summary(LocalStore(tmp_path / "state.sqlite"), **kwargs)


def test_cli_missing_store_does_not_create_ledger(tmp_path):
    with pytest.raises(sqlite3.OperationalError):
        run(_parser().parse_args(["--home", str(tmp_path), "beta-summary"]))
    assert not (tmp_path / "state.sqlite").exists()
