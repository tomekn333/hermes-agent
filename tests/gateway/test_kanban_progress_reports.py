"""Evidence-based Kanban progress reports (card t_d283fa6a).

Covers the owner's hard requirements: a report only after a REAL commit or
an explicitly declared stage, no history replay on first sight, no
duplication after a restart, bounded silence notes, coalescing of commit
bursts, silence for done/archived tasks, and the origin-thread routing
inherited from card t_08050af3.
"""

import subprocess
import time
import types

import pytest

from gateway.kanban_progress import (
    BASELINE_EVENT,
    NO_STAGE_TEXT,
    PROGRESS_EVENT,
    STATE_NO_PROGRESS,
    STATE_WORKING,
    ProgressSettings,
    apply_decision,
    commits_since,
    decide_for_task,
    declared_stage,
    format_event_for_delivery,
    head_sha,
    render_progress_message,
    scan_board,
    stage_from_workspace,
)


# --- helpers ---------------------------------------------------------------


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def repo(tmp_path):
    """A real git repository with one commit — no mocked subprocess."""
    path = tmp_path / "wt"
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "T")
    (path / "a.txt").write_text("1")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "pierwszy commit")
    return path


def _commit(repo, message, filename="a.txt"):
    (repo / filename).write_text(str(time.time_ns()))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return head_sha(str(repo))


def _task(**kw):
    base = dict(
        id="t_test", status="running", workspace_path=None, current_run_id=1
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


def _event(kind, payload=None, created_at=None, run_id=1):
    return types.SimpleNamespace(
        kind=kind,
        payload=payload,
        created_at=int(created_at if created_at is not None else time.time()),
        run_id=run_id,
    )


# --- git evidence ----------------------------------------------------------


def test_head_sha_reads_a_real_repo(repo):
    sha = head_sha(str(repo))
    assert sha and len(sha) >= 7


def test_head_sha_is_none_outside_a_repo(tmp_path):
    assert head_sha(str(tmp_path / "nope")) is None
    assert head_sha(None) is None


def test_commits_since_returns_only_new_commits_oldest_first(repo):
    base = head_sha(str(repo))
    _commit(repo, "drugi")
    _commit(repo, "trzeci")
    out = commits_since(str(repo), base)
    assert [c["subject"] for c in out] == ["drugi", "trzeci"]


def test_commits_since_unknown_base_returns_nothing_not_full_history(repo):
    """A rebased/force-moved cursor must NOT replay the whole history."""
    _commit(repo, "drugi")
    assert commits_since(str(repo), "0" * 40) == []


# --- declared stage --------------------------------------------------------


def test_stage_comes_from_worker_declaration():
    now = int(time.time())
    events = [
        _event("heartbeat", {"note": "pisze testy dedupe"}, created_at=now - 60),
    ]
    assert declared_stage(events, now=now, max_age_minutes=30) == "pisze testy dedupe"


def test_stale_stage_declaration_is_discarded():
    now = int(time.time())
    events = [_event("heartbeat", {"note": "stary etap"}, created_at=now - 7200)]
    assert declared_stage(events, now=now, max_age_minutes=30) is None


def test_newest_declaration_wins():
    now = int(time.time())
    events = [
        _event("heartbeat", {"note": "stary"}, created_at=now - 600),
        _event("progress_stage", {"stage": "nowy"}, created_at=now - 10),
    ]
    assert declared_stage(events, now=now, max_age_minutes=30) == "nowy"


def test_commit_subject_is_never_used_as_the_current_stage():
    """The stage must not be inferred from the previous commit."""
    now = int(time.time())
    events = [_event(PROGRESS_EVENT, {"commits": [{"sha": "abc", "subject": "fix X"}]})]
    assert declared_stage(events, now=now, max_age_minutes=30) is None


def test_stage_file_lane(tmp_path):
    ws = tmp_path / "ws"
    (ws / ".hermes").mkdir(parents=True)
    (ws / ".hermes" / "stage.txt").write_text("buduje obraz\n")
    now = int(time.time())
    assert stage_from_workspace(str(ws), now=now, max_age_minutes=30) == "buduje obraz"


# --- rendering -------------------------------------------------------------


def test_render_matches_the_requested_shape():
    msg = render_progress_message(
        {
            "state": STATE_WORKING,
            "commits": [{"sha": "abc1234", "subject": "dodano parser"}],
            "stage": "pisze testy",
        }
    )
    assert msg == "Zapisano abc1234: dodano parser. Praca trwa. Teraz: pisze testy"


def test_render_is_honest_when_no_stage_was_declared():
    msg = render_progress_message(
        {"state": STATE_WORKING, "commits": [{"sha": "a", "subject": "b"}]}
    )
    assert NO_STAGE_TEXT in msg


def test_render_silence_says_no_confirmed_progress_not_fake_activity():
    msg = render_progress_message({"state": STATE_NO_PROGRESS, "commits": []})
    assert "Brak nowego potwierdzonego postepu." in msg
    assert "Praca trwa" not in msg


def test_render_coalesces_a_burst_with_an_explicit_remainder_count():
    msg = render_progress_message(
        {
            "state": STATE_WORKING,
            "commits": [
                {"sha": "a1", "subject": "x"},
                {"sha": "a2", "subject": "y"},
            ],
            "commits_omitted": 4,
            "stage": "s",
        }
    )
    assert "a1: x" in msg and "a2: y" in msg and "(+4 dalszych commitow)" in msg


def test_render_contains_no_percentages_or_log_dumps():
    msg = render_progress_message(
        {"state": STATE_WORKING, "commits": [{"sha": "a", "subject": "b"}], "stage": "c"}
    )
    assert "%" not in msg
    assert len(msg.splitlines()) == 1


def test_delivery_formatter_prefers_the_frozen_text():
    assert format_event_for_delivery({"text": "zamrozony tekst"}) == "zamrozony tekst"
    # Legacy row without `text` still renders.
    assert "Zapisano" in format_event_for_delivery(
        {"state": STATE_WORKING, "commits": [{"sha": "a", "subject": "b"}]}
    )


# --- decision: baseline / replay -------------------------------------------


def test_first_observation_baselines_instead_of_replaying_history(repo):
    _commit(repo, "drugi")
    _commit(repo, "trzeci")
    d = decide_for_task(
        task=_task(workspace_path=str(repo)),
        events=[],
        settings=ProgressSettings(),
        now=int(time.time()),
    )
    assert d.action == "baseline"
    assert d.baseline_sha == head_sha(str(repo))


def test_no_report_when_head_has_not_moved(repo):
    now = int(time.time())
    events = [
        _event(BASELINE_EVENT, {"head_sha": head_sha(str(repo))}, created_at=now - 30)
    ]
    d = decide_for_task(
        task=_task(workspace_path=str(repo)),
        events=events,
        settings=ProgressSettings(),
        now=now,
    )
    assert d.action == "skip"


def test_new_commit_produces_a_report_with_the_real_sha(repo):
    now = int(time.time())
    base = head_sha(str(repo))
    events = [_event(BASELINE_EVENT, {"head_sha": base}, created_at=now - 600)]
    new_sha = _commit(repo, "realna zmiana")
    d = decide_for_task(
        task=_task(workspace_path=str(repo)),
        events=events,
        settings=ProgressSettings(),
        now=now,
    )
    assert d.action == "report"
    assert d.payload["commits"][0]["subject"] == "realna zmiana"
    assert new_sha.startswith(d.payload["commits"][0]["sha"])
    assert d.payload["head_sha"] == new_sha


def test_report_carries_the_declared_stage_only(repo):
    now = int(time.time())
    base = head_sha(str(repo))
    events = [
        _event(BASELINE_EVENT, {"head_sha": base}, created_at=now - 600),
        _event("heartbeat", {"note": "teraz migracja"}, created_at=now - 20),
    ]
    _commit(repo, "zmiana")
    d = decide_for_task(
        task=_task(workspace_path=str(repo)),
        events=events,
        settings=ProgressSettings(),
        now=now,
    )
    assert d.payload["stage"] == "teraz migracja"


def test_stage_is_none_when_worker_declared_nothing(repo):
    now = int(time.time())
    events = [_event(BASELINE_EVENT, {"head_sha": head_sha(str(repo))}, created_at=now - 600)]
    _commit(repo, "zmiana")
    d = decide_for_task(
        task=_task(workspace_path=str(repo)),
        events=events,
        settings=ProgressSettings(),
        now=now,
    )
    assert d.payload["stage"] is None
    assert NO_STAGE_TEXT in render_progress_message(d.payload)


# --- dedupe / throttling / restart ------------------------------------------


def test_same_commit_is_not_reported_twice(repo):
    now = int(time.time())
    base = head_sha(str(repo))
    events = [_event(BASELINE_EVENT, {"head_sha": base}, created_at=now - 600)]
    _commit(repo, "raz")
    first = decide_for_task(
        task=_task(workspace_path=str(repo)),
        events=events,
        settings=ProgressSettings(),
        now=now,
    )
    assert first.action == "report"
    # Simulate the write: the cursor is the event itself.
    events.append(_event(PROGRESS_EVENT, first.payload, created_at=now))
    second = decide_for_task(
        task=_task(workspace_path=str(repo)),
        events=events,
        settings=ProgressSettings(),
        now=now + 5,
    )
    assert second.action == "skip"


def test_restart_does_not_duplicate_because_the_cursor_is_in_the_board(repo):
    """State lives in task_events, so a fresh process re-reads, never replays."""
    now = int(time.time())
    base = head_sha(str(repo))
    _commit(repo, "raz")
    events = [
        _event(BASELINE_EVENT, {"head_sha": base}, created_at=now - 600),
        _event(
            PROGRESS_EVENT,
            {"head_sha": head_sha(str(repo)), "state": STATE_WORKING, "commits": []},
            created_at=now - 10,
        ),
    ]
    # "New process": no in-memory state at all, only the board.
    d = decide_for_task(
        task=_task(workspace_path=str(repo)),
        events=events,
        settings=ProgressSettings(),
        now=now,
    )
    assert d.action == "skip"


def test_commit_burst_is_coalesced_into_one_report(repo):
    now = int(time.time())
    base = head_sha(str(repo))
    events = [_event(BASELINE_EVENT, {"head_sha": base}, created_at=now - 600)]
    for i in range(5):
        _commit(repo, f"commit {i}")
    settings = ProgressSettings(max_commits_per_report=2)
    d = decide_for_task(
        task=_task(workspace_path=str(repo)),
        events=events,
        settings=settings,
        now=now,
    )
    assert d.action == "report"
    assert len(d.payload["commits"]) == 2
    assert d.payload["commits_omitted"] == 3


def test_throttle_window_suppresses_a_second_report(repo):
    now = int(time.time())
    base = head_sha(str(repo))
    events = [_event(PROGRESS_EVENT, {"head_sha": base}, created_at=now - 10)]
    _commit(repo, "szybki kolejny")
    d = decide_for_task(
        task=_task(workspace_path=str(repo)),
        events=events,
        settings=ProgressSettings(min_interval_seconds=120),
        now=now,
    )
    assert d.action == "skip"
    assert d.reason == "throttled"
    # ...and the same evidence IS reported once the window closes.
    later = decide_for_task(
        task=_task(workspace_path=str(repo)),
        events=events,
        settings=ProgressSettings(min_interval_seconds=120),
        now=now + 200,
    )
    assert later.action == "report"


# --- silence ---------------------------------------------------------------


def test_long_silence_reports_no_confirmed_progress_not_invented_coding(repo):
    now = int(time.time())
    events = [
        _event(PROGRESS_EVENT, {"head_sha": head_sha(str(repo))}, created_at=now - 3600)
    ]
    d = decide_for_task(
        task=_task(workspace_path=str(repo)),
        events=events,
        settings=ProgressSettings(silence_minutes=10),
        now=now,
    )
    assert d.action == "report"
    assert d.payload["state"] == STATE_NO_PROGRESS
    text = render_progress_message(d.payload)
    assert "Brak nowego potwierdzonego postepu." in text
    assert "koduje" not in text


def test_silence_note_does_not_repeat_every_tick(repo):
    now = int(time.time())
    events = [
        _event(
            PROGRESS_EVENT,
            {"head_sha": head_sha(str(repo)), "state": STATE_NO_PROGRESS},
            created_at=now - 3600,
        )
    ]
    d = decide_for_task(
        task=_task(workspace_path=str(repo)),
        events=events,
        settings=ProgressSettings(silence_minutes=10),
        now=now,
    )
    assert d.action == "skip"


def test_silence_window_is_configurable(repo):
    now = int(time.time())
    events = [
        _event(PROGRESS_EVENT, {"head_sha": head_sha(str(repo))}, created_at=now - 300)
    ]
    assert (
        decide_for_task(
            task=_task(workspace_path=str(repo)),
            events=events,
            settings=ProgressSettings(silence_minutes=10),
            now=now,
        ).action
        == "skip"
    )
    assert (
        decide_for_task(
            task=_task(workspace_path=str(repo)),
            events=events,
            settings=ProgressSettings(silence_minutes=2),
            now=now,
        ).action
        == "report"
    )


# --- terminal states -------------------------------------------------------


@pytest.mark.parametrize("status", ["done", "archived", "blocked", "triage", "ready"])
def test_no_progress_report_for_non_running_tasks(repo, status):
    now = int(time.time())
    events = [_event(PROGRESS_EVENT, {"head_sha": "old"}, created_at=now - 99999)]
    _commit(repo, "zmiana po zakonczeniu")
    d = decide_for_task(
        task=_task(status=status, workspace_path=str(repo)),
        events=events,
        settings=ProgressSettings(),
        now=now,
    )
    assert d.action == "skip"


# --- settings --------------------------------------------------------------


def test_settings_read_from_config_and_fail_safe_on_garbage():
    s = ProgressSettings.from_config(
        {"kanban": {"progress_reports": {"enabled": False, "silence_minutes": 3}}}
    )
    assert s.enabled is False and s.silence_minutes == 3
    bad = ProgressSettings.from_config(
        {"kanban": {"progress_reports": {"silence_minutes": "nonsense"}}}
    )
    assert bad.silence_minutes == ProgressSettings().silence_minutes
    assert ProgressSettings.from_config(None).enabled is True


def test_shipped_config_default_has_the_progress_section():
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    section = DEFAULT_CONFIG["kanban"]["progress_reports"]
    assert section["enabled"] is True
    assert section["silence_minutes"] >= 1
    assert section["min_interval_seconds"] >= 0


# --- board integration -----------------------------------------------------


def test_scan_board_writes_baseline_then_report_on_a_real_board(repo):
    from hermes_cli import kanban_db as kb

    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="progres test",
            assignee="default",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='running' WHERE id=?", (task_id,)
            )
        settings = ProgressSettings(min_interval_seconds=0, silence_minutes=1)

        tally = scan_board(conn, kb=kb, settings=settings)
        assert tally["baselined"] == 1 and tally["reported"] == 0
        kinds = [e.kind for e in kb.list_events(conn, task_id)]
        assert BASELINE_EVENT in kinds and PROGRESS_EVENT not in kinds

        # No movement → no report.
        assert scan_board(conn, kb=kb, settings=settings)["reported"] == 0

        _commit(repo, "prawdziwy commit")
        assert scan_board(conn, kb=kb, settings=settings)["reported"] == 1
        progress = [
            e for e in kb.list_events(conn, task_id) if e.kind == PROGRESS_EVENT
        ]
        assert len(progress) == 1
        assert "prawdziwy commit" in progress[0].payload["text"]
        assert progress[0].payload["text"].startswith("Zapisano ")

        # Idempotent: running again adds nothing.
        assert scan_board(conn, kb=kb, settings=settings)["reported"] == 0
        assert (
            len([e for e in kb.list_events(conn, task_id) if e.kind == PROGRESS_EVENT])
            == 1
        )
    finally:
        conn.close()


def test_apply_decision_skip_writes_nothing():
    calls = []
    fake_kb = types.SimpleNamespace(
        write_txn=lambda conn: (_ for _ in ()).throw(AssertionError("no txn")),
        _append_event=lambda *a, **k: calls.append(a),
    )
    from gateway.kanban_progress import ProgressDecision

    assert apply_decision(None, "t_x", ProgressDecision("skip"), fake_kb) is False
    assert calls == []


# --- integration: event -> payload -> gateway delivery ---------------------


class _RecordingAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})

    async def handle_message(self, event):  # pragma: no cover - unused here
        pass


def _make_runner(adapter):
    from gateway.config import Platform
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.SLACK: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    return runner


async def _one_notifier_tick(monkeypatch, runner):
    import asyncio

    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _live_board(tmp_path, monkeypatch, name, repo):
    """A board with one running task subscribed from a Slack origin thread."""
    from hermes_cli import kanban_db as kb

    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / name))
    kb.init_db()
    conn = kb.connect()
    tid = kb.create_task(
        conn,
        title="zadanie w toku",
        assignee="default",
        workspace_kind="worktree",
        workspace_path=str(repo),
    )
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (tid,))
    kb.add_notify_sub(
        conn,
        task_id=tid,
        platform="slack",
        chat_id="C_ORIGIN",
        thread_id="1790511720.697219",
    )
    return kb, conn, tid


def test_progress_event_is_delivered_into_the_origin_thread(
    tmp_path, monkeypatch, repo
):
    """Full chain: real commit -> progress event -> Slack send in the thread."""
    import asyncio

    kb, conn, tid = _live_board(tmp_path, monkeypatch, "progress-thread.db", repo)
    settings = ProgressSettings(min_interval_seconds=0)
    try:
        scan_board(conn, kb=kb, settings=settings)  # baseline
        _commit(repo, "dodano walidacje wejscia")
        assert scan_board(conn, kb=kb, settings=settings)["reported"] == 1
    finally:
        conn.close()

    adapter = _RecordingAdapter()
    asyncio.run(_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 1
    sent = adapter.sent[0]
    assert sent["chat_id"] == "C_ORIGIN"
    assert sent["metadata"]["thread_id"] == "1790511720.697219"
    assert sent["metadata"].get("reply_broadcast") in (None, False)
    assert "Zapisano " in sent["text"]
    assert "dodano walidacje wejscia" in sent["text"]
    assert "Praca trwa." in sent["text"]


def test_baseline_event_is_never_delivered(tmp_path, monkeypatch, repo):
    """The cursor row must not become a user-visible message."""
    import asyncio

    kb, conn, tid = _live_board(tmp_path, monkeypatch, "baseline-silent.db", repo)
    try:
        tally = scan_board(conn, kb=kb, settings=ProgressSettings())
        assert tally["baselined"] == 1
    finally:
        conn.close()

    adapter = _RecordingAdapter()
    asyncio.run(_one_notifier_tick(monkeypatch, _make_runner(adapter)))
    assert adapter.sent == []


def test_progress_without_an_origin_thread_is_withheld_not_broadcast(
    tmp_path, monkeypatch, repo
):
    """No origin anchor on Slack => pending, never a new top-level message."""
    import asyncio

    from hermes_cli import kanban_db as kb

    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "progress-orphan.db"))
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="bez watku",
            assignee="default",
            workspace_kind="worktree",
            workspace_path=str(repo),
        )
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (tid,))
        kb.add_notify_sub(
            conn, task_id=tid, platform="slack", chat_id="C_NOTHREAD", thread_id=""
        )
        settings = ProgressSettings(min_interval_seconds=0)
        scan_board(conn, kb=kb, settings=settings)
        _commit(repo, "zmiana bez watku")
        assert scan_board(conn, kb=kb, settings=settings)["reported"] == 1
    finally:
        conn.close()

    adapter = _RecordingAdapter()
    asyncio.run(_one_notifier_tick(monkeypatch, _make_runner(adapter)))
    assert adapter.sent == []


def test_progress_is_not_delivered_after_the_task_is_done(
    tmp_path, monkeypatch, repo
):
    """A stale progress row must not post 'work continues' after DONE."""
    import asyncio

    kb, conn, tid = _live_board(tmp_path, monkeypatch, "progress-done.db", repo)
    settings = ProgressSettings(min_interval_seconds=0)
    try:
        scan_board(conn, kb=kb, settings=settings)
        _commit(repo, "ostatnia zmiana")
        assert scan_board(conn, kb=kb, settings=settings)["reported"] == 1
        kb.complete_task(conn, tid, summary="gotowe")
    finally:
        conn.close()

    adapter = _RecordingAdapter()
    asyncio.run(_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    texts = [s["text"] for s in adapter.sent]
    assert not any("Praca trwa." in t for t in texts)
    assert any("done" in t for t in texts)


def test_a_restarted_notifier_does_not_redeliver_a_progress_report(
    tmp_path, monkeypatch, repo
):
    """The claim cursor lives in the DB, so a second tick sends nothing."""
    import asyncio

    kb, conn, tid = _live_board(tmp_path, monkeypatch, "progress-restart.db", repo)
    settings = ProgressSettings(min_interval_seconds=0)
    try:
        scan_board(conn, kb=kb, settings=settings)
        _commit(repo, "jedyny commit")
        scan_board(conn, kb=kb, settings=settings)
    finally:
        conn.close()

    first = _RecordingAdapter()
    asyncio.run(_one_notifier_tick(monkeypatch, _make_runner(first)))
    assert len(first.sent) == 1

    # Fresh runner == fresh process state.
    second = _RecordingAdapter()
    asyncio.run(_one_notifier_tick(monkeypatch, _make_runner(second)))
    assert second.sent == []


def test_transient_send_failure_rewinds_and_retries_next_tick(
    tmp_path, monkeypatch, repo
):
    """A Slack 429 (send raises) must not lose the report."""
    import asyncio

    kb, conn, tid = _live_board(tmp_path, monkeypatch, "progress-429.db", repo)
    settings = ProgressSettings(min_interval_seconds=0)
    try:
        scan_board(conn, kb=kb, settings=settings)
        _commit(repo, "commit pod 429")
        scan_board(conn, kb=kb, settings=settings)
    finally:
        conn.close()

    class _RateLimited(_RecordingAdapter):
        async def send(self, chat_id, text, metadata=None):
            raise RuntimeError("ratelimited: 429")

    asyncio.run(_one_notifier_tick(monkeypatch, _make_runner(_RateLimited())))

    # The claim was rewound, so a healthy adapter still delivers it.
    healthy = _RecordingAdapter()
    asyncio.run(_one_notifier_tick(monkeypatch, _make_runner(healthy)))
    assert len(healthy.sent) == 1
    assert "commit pod 429" in healthy.sent[0]["text"]

