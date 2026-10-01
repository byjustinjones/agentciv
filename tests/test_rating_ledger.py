"""Each finished game changes each rating pool exactly once, whatever fails and
however often the server restarts (leaderboard format 2 with an ``applied``
ledger; docs/DESIGN.md §12 "Server restarts")."""
from __future__ import annotations

import json
import logging
from unittest.mock import patch

import pytest

from agentciv.server import storage as storage_mod
from agentciv.server.manager import GameManager, GameSession
from agentciv.server.storage import Storage


@pytest.fixture(autouse=True)
def _quiet():
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


@pytest.fixture
def no_workers():
    """Drive the session lifecycle synchronously (no worker threads)."""
    with patch.object(GameSession, "launch", lambda self: None):
        yield


def finished_session(data_dir, fog=False):
    """A 2-player rated game, resolved to its end and checkpointed, not yet finalized."""
    manager = GameManager(str(data_dir), open_ratings=True, restore=False)
    session = manager.create_game({"max_players": 2, "max_turns": 1, "turn_timeout": 30, "fog": fog})
    session.join("Alice")
    session.join("Bob")
    with session.cond:
        session._advance()
    assert session.status == "finished"
    session.checkpoint(force=True, bots_idle=True)
    return manager, session


def games(manager, pool="standard"):
    return {r["name"]: r["games"] for r in manager.storage.leaderboard(pool)}


def restart(data_dir):
    """A new manager on the same data dir; finalize what it restored, as its workers would."""
    m = GameManager(str(data_dir), open_ratings=True)
    for s in list(m.sessions.values()):
        assert s._finalize()
    return m


class Crash(BaseException):
    """The process dies here (not an error the server could catch)."""


@pytest.mark.parametrize("failure", ["save_replay", "record_result"])
def test_failed_write_rates_exactly_once_after_restart(tmp_path, no_workers, failure):
    manager, session = finished_session(tmp_path)
    gid = session.game_id
    with patch.object(manager.storage, failure, side_effect=OSError("injected failure")):
        assert session._finalize() is False
    assert manager.live.ids() == [gid]  # checkpoint kept
    assert session._fin_due is not None and not session._fin_done
    expect = {"Alice": 1, "Bob": 1} if failure == "save_replay" else {}
    assert games(manager) == expect
    manager.shutdown()
    m2 = restart(tmp_path)
    assert games(m2) == {"Alice": 1, "Bob": 1}
    assert m2.live.ids() == []
    rating = m2.storage.summary(gid)["rating"]
    assert rating["pool"] == "standard" and sorted(n for n, _ in rating["entries"]) == ["Alice", "Bob"]
    m2.shutdown()
    m3 = restart(tmp_path)  # and once more: nothing changes
    assert games(m3) == {"Alice": 1, "Bob": 1} and m3.restored == []
    m3.shutdown()


def test_crash_between_rating_and_replay(tmp_path, no_workers):
    manager, session = finished_session(tmp_path)
    with patch.object(manager.storage, "save_replay", side_effect=Crash()):
        with pytest.raises(Crash):
            session._finalize()
    # no orderly shutdown: the process is gone
    m2 = restart(tmp_path)
    assert m2.restored == [session.game_id]
    assert games(m2) == {"Alice": 1, "Bob": 1}
    assert m2.storage.summary(session.game_id) is not None and m2.live.ids() == []
    m2.shutdown()


def test_crash_after_replay_before_checkpoint_delete(tmp_path, no_workers):
    manager, session = finished_session(tmp_path)
    gid = session.game_id
    with patch.object(GameSession, "discard_checkpoint", side_effect=Crash()):
        with pytest.raises(Crash):
            session._finalize()
    assert manager.live.ids() == [gid] and manager.storage.summary(gid) is not None
    m2 = restart(tmp_path)
    assert m2.restored == [] and m2.live.ids() == []
    assert games(m2) == {"Alice": 1, "Bob": 1}
    m2.shutdown()


def test_saved_replay_without_durable_rating_is_rated_from_its_summary(tmp_path, no_workers):
    """The checkpoint of a saved game is deleted only once the pool has its rating."""
    manager, session = finished_session(tmp_path)
    gid = session.game_id
    with patch.object(GameSession, "discard_checkpoint", lambda self: None):
        assert session._finalize()
    # lose the rating (e.g. a leaderboard restored from an older backup)
    (tmp_path / "leaderboard.json").write_text(json.dumps({"format": 2, "players": {}, "applied": []}))
    with patch.object(Storage, "record_result", side_effect=OSError("disk full")):
        m2 = GameManager(str(tmp_path), open_ratings=True)
    assert m2.live.ids() == [gid] and games(m2) == {}  # kept for a retry
    m2._sweep()
    assert m2.live.ids() == [gid]                      # not due yet
    m2._unfinished[gid][1] = 0.0
    m2._sweep()
    assert m2.live.ids() == [] and games(m2) == {"Alice": 1, "Bob": 1} and not m2._unfinished
    m2.shutdown()


def test_double_finalize_rates_once(tmp_path, no_workers):
    manager, session = finished_session(tmp_path)
    assert session._finalize() and session._finalize()
    # even if the session forgot it had finished, the ledger refuses a second rating
    session._fin_done = session._rating_done = session.saved = False
    session._ckpt_disabled = False
    session.frames = None
    assert session._finalize()
    assert games(manager) == {"Alice": 1, "Bob": 1}
    lb = json.loads((tmp_path / "leaderboard.json").read_text())
    assert lb["applied"] == [session.game_id]
    manager.shutdown()


def test_failed_finalize_is_retried_from_sweep(tmp_path, no_workers):
    manager, session = finished_session(tmp_path)
    gid = session.game_id
    with patch.object(manager.storage, "save_replay", side_effect=OSError("injected failure")):
        assert session._finalize() is False
        manager._retire(session)          # what the worker does next
        session._fin_due = 0.0
        manager._sweep()                  # still failing: backoff grows
    assert session._fin_attempts == 2 and session._fin_due > 0 and gid in manager.sessions
    assert manager.live.ids() == [gid]
    manager._sweep()                      # not due yet
    assert not session._fin_done
    session._fin_due = 0.0
    manager._sweep()
    assert session._fin_done and session.saved and manager.live.ids() == []
    assert gid in manager._finished
    assert games(manager) == {"Alice": 1, "Bob": 1}
    manager.shutdown()


def test_fog_pool_failure_rates_once(tmp_path, no_workers):
    manager, session = finished_session(tmp_path, fog=True)
    with patch.object(manager.storage, "record_result", side_effect=OSError("injected failure")):
        assert session._finalize() is False
    manager.shutdown()
    m2 = restart(tmp_path)
    assert games(m2, "fog") == {"Alice": 1, "Bob": 1} and games(m2) == {}
    fog = json.loads((tmp_path / "leaderboard_fog.json").read_text())
    assert fog["applied"] == [session.game_id]
    assert not (tmp_path / "leaderboard.json").exists()
    assert m2.storage.summary(session.game_id)["rating"]["pool"] == "fog"
    m2.shutdown()


# ---------------------------------------------------------------- storage
def test_legacy_flat_leaderboard_is_migrated(tmp_path):
    flat = {"Alice": {"mu": 27.0, "sigma": 7.0, "games": 3, "wins": 2, "total_place": 4},
            "Bob": {"mu": 23.0, "sigma": 7.5, "games": 3, "wins": 1, "total_place": 5}}
    (tmp_path / "leaderboard.json").write_text(json.dumps(flat))
    (tmp_path / "replays").mkdir()
    (tmp_path / "replays" / "g1.json").write_text(json.dumps(
        {"game_id": "g1", "summary": {"game_id": "g1", "status": "finished"}, "result": None, "frames": []}))
    st = Storage(tmp_path)
    assert st.table == flat                     # in-memory shape unchanged
    assert st.is_applied("g1") and st.is_applied("g1", "fog")
    assert st.record_result("g1", ["Alice", "Bob"]) is False  # archived: rated by the old code
    assert st.table == flat
    assert st.record_result("g2", ["Bob", "Alice"]) is True
    data = json.loads((tmp_path / "leaderboard.json").read_text())
    assert data["format"] == 2 and data["applied"] == ["g1", "g2"]
    assert data["players"]["Alice"]["games"] == 4 and data["players"] == st.table
    assert json.loads((tmp_path / "leaderboard.json.v1.bak").read_text()) == flat
    st2 = Storage(tmp_path)
    assert st2.table == st.table and st2.is_applied("g2") and not st2.is_applied("g2", "fog")
    assert [r["name"] for r in st2.leaderboard()] == [r["name"] for r in st.leaderboard()]


def test_record_result_is_idempotent_and_transactional(tmp_path):
    st = Storage(tmp_path)
    with patch.object(storage_mod, "_atomic_write", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            st.record_result("g7", ["Alice", "Bob"])
    assert st.table == {} and not st.is_applied("g7")
    assert not (tmp_path / "leaderboard.json").exists()
    assert st.record_result("g7", ["Alice", "Bob"]) is True
    snap = json.dumps(st.table, sort_keys=True)
    assert st.record_result("g7", ["Bob", "Alice"]) is False
    assert json.dumps(st.table, sort_keys=True) == snap
    assert st.record_result("g8", ["idle", "idle"]) is False  # one identity: nothing to rate
    assert st.record_result("g9", ["idle", "Alice", "idle"]) is True
    assert st.table["idle"]["games"] == 2 and st.table["Alice"]["games"] == 2


def test_replay_is_the_same_before_and_after_it_is_saved(tmp_path, no_workers):
    """A finished game's replay served from memory (before the finalization)
    equals the saved file, ``rating`` in the summary included."""
    manager, session = finished_session(tmp_path)
    live = session.replay_bytes()
    assert json.loads(live)["summary"]["rating"]["entries"]
    assert session._finalize()
    assert session.replay_bytes() == live == manager.storage.read_replay(session.game_id)
    manager.shutdown()
