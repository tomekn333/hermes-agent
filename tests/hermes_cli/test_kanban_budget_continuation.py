"""Lokalny patch 2026-10-08: ciaglosc po wyczerpaniu budzetu iteracji."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _repo(tmp_path: Path) -> Path:
    r = tmp_path / "ws"
    r.mkdir()
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin"}
    subprocess.run(["git", "init", "-q", str(r)], check=True, env=env)
    return r


def _commit(repo: Path, name: str) -> None:
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin"}
    (repo / name).write_text(name)
    subprocess.run(["git", "-C", str(repo), "add", name], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", f"wip {name}"], check=True, env=env)


def _start(conn, tid):
    kb.claim_task(conn, tid)
    run_id = kb.get_task(conn, tid).current_run_id
    # run "zaczal sie" sekunde temu, zeby commit byl na pewno pozniej
    conn.execute("UPDATE task_runs SET started_at = started_at - 2 WHERE id = ?", (run_id,))
    conn.commit()
    return run_id


def _run(conn, run_id):
    return conn.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,)).fetchone()


def test_progress_means_continuation_with_handoff(kanban_home, tmp_path):
    repo = _repo(tmp_path)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="skeletony")
        run_id = _start(conn, tid)
        _commit(repo, "a.txt")
        res = kb.record_budget_exhausted(conn, tid, summary="ZROBIONE: x\nNASTEPNY KROK: y",
                                         used=200, max_iterations=200, workspace=str(repo))
        assert res["action"] == "continued"
        t = kb.get_task(conn, tid)
        assert t.status == "ready"
        assert t.consecutive_failures == 0
        r = _run(conn, run_id)
        assert r["summary"].startswith("ZROBIONE")
        assert json.loads(r["metadata"])["continuation"] is True
        kinds = [e["kind"] for e in conn.execute("SELECT kind FROM task_events WHERE task_id=?", (tid,))]
        assert "continued" in kinds and "gave_up" not in kinds
        # handoff widoczny dla nastepnego workera
        assert "NASTEPNY KROK: y" in kb.build_worker_context(conn, tid)


def test_no_progress_counts_as_failure_and_keeps_handoff(kanban_home, tmp_path):
    repo = _repo(tmp_path)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="bez postepu")
        run1 = _start(conn, tid)
        res = kb.record_budget_exhausted(conn, tid, summary="PULAPKI: test wisi",
                                         used=200, max_iterations=200, workspace=str(repo))
        assert res["action"] == "retry"
        assert kb.get_task(conn, tid).consecutive_failures == 1
        assert _run(conn, run1)["summary"] == "PULAPKI: test wisi"
        _start(conn, tid)
        res = kb.record_budget_exhausted(conn, tid, summary="nadal nic",
                                         used=200, max_iterations=200, workspace=str(repo))
        assert res["action"] == "blocked"
        assert kb.get_task(conn, tid).status == "blocked"
        ev = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='gave_up'", (tid,)).fetchone()
        p = json.loads(ev["payload"])
        assert p["reason_kind"] == "budget_exhausted"
        assert p["handoff"] == "nadal nic"
        assert "kontynuuj" in p["decision"]


def test_continuation_limit_trips_to_blocked(kanban_home, tmp_path):
    repo = _repo(tmp_path)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="duze zadanie")
        for i in range(2):
            _start(conn, tid)
            _commit(repo, f"f{i}.txt")
            assert kb.record_budget_exhausted(conn, tid, summary=f"s{i}", used=200, max_iterations=200,
                                              workspace=str(repo), continuation_limit=2)["action"] == "continued"
        _start(conn, tid)
        _commit(repo, "f9.txt")
        res = kb.record_budget_exhausted(conn, tid, summary="s9", used=200, max_iterations=200,
                                         workspace=str(repo), continuation_limit=2)
        assert res["action"] == "blocked"
        assert kb.get_task(conn, tid).status == "blocked"


def test_checkpoint_nudge_once_and_only_in_kanban(monkeypatch):
    from agent import kanban_continuity as kc

    class A:
        max_iterations = 200

    a = A()
    msgs = [{"role": "tool", "content": "out"}]
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert kc.maybe_inject_checkpoint(a, msgs, 190) is False
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_x")
    assert kc.maybe_inject_checkpoint(a, msgs, 100) is False
    assert kc.maybe_inject_checkpoint(a, msgs, 170) is True
    assert kc.CHECKPOINT_MARKER in msgs[-1]["content"]
    msgs.append({"role": "tool", "content": "out2"})
    assert kc.maybe_inject_checkpoint(a, msgs, 180) is False
    assert kc.summary_request("default") == kc.HANDOFF_REQUEST
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    assert kc.summary_request("default") == "default"


def test_budget_block_survives_recompute_ready(kanban_home, tmp_path):
    repo = _repo(tmp_path)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="stop")
        for _ in range(2):
            _start(conn, tid)
            kb.record_budget_exhausted(conn, tid, summary="x", used=200, max_iterations=200, workspace=str(repo))
        assert kb.get_task(conn, tid).status == "blocked"
        kb.recompute_ready(conn)
        assert kb.get_task(conn, tid).status == "blocked"
