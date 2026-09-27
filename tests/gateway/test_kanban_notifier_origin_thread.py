"""Slack task reports must land in the origin request thread, never top-level.

Card t_08050af3 (owner decision 2026-09-27) reverses the earlier
root-forcing policy from t_ab93e192 / commit 72d170c85: progress, DONE,
BLOCKED, review and deploy reports about a *specific task* belong in the
thread that carries the original request. A threadless Slack send creates a
brand-new channel message, so the notifier withholds such a report instead
of guessing an anchor, and only an explicitly flagged system-announcement
lane may post at the channel root.
"""

import asyncio

from gateway.config import Platform
from gateway.kanban_watchers import (
    _ALLOW_TOP_LEVEL_KEY,
    _origin_thread_missing,
    _thread_only_platforms,
)
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


class RecordingAdapter:
    def __init__(self):
        self.sent = []
        self.handled = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})

    async def handle_message(self, event):
        self.handled.append(event)


async def _run_one_notifier_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _make_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.SLACK: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    return runner


def _unseen(tid, chat_id, thread_id=""):
    conn = kb.connect()
    try:
        _, events = kb.unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="slack",
            chat_id=chat_id,
            thread_id=thread_id,
            kinds=[
                "completed",
                "blocked",
                "gave_up",
                "crashed",
                "timed_out",
                "review_requested",
                "changes_requested",
            ],
        )
        return events
    finally:
        conn.close()


# --- unit level: the gate predicate itself --------------------------------


def test_slack_is_thread_only_by_default():
    assert "slack" in _thread_only_platforms()


def test_gate_fires_only_without_an_anchor_on_a_thread_only_platform():
    # No anchor on Slack -> withhold.
    assert _origin_thread_missing("slack", {}, {}) is True
    # thread_id present -> deliver.
    assert _origin_thread_missing("slack", {"thread_id": "1.2"}, {}) is False
    # thread_ts alias present -> deliver.
    assert _origin_thread_missing("slack", {"thread_ts": "1.2"}, {}) is False
    # Another platform keeps its own semantics.
    assert _origin_thread_missing("telegram", {}, {}) is False


def test_explicit_system_announcement_lane_may_post_top_level():
    sub = {"delivery_metadata": {_ALLOW_TOP_LEVEL_KEY: True}}
    assert _origin_thread_missing("slack", {}, sub) is False
    assert _origin_thread_missing("slack", {_ALLOW_TOP_LEVEL_KEY: True}, {}) is False


# --- integration: the notifier tick ---------------------------------------


def _subscribe(conn, tid, *, thread_id="", delivery_metadata=None):
    kb.add_notify_sub(
        conn,
        task_id=tid,
        platform="slack",
        chat_id="C_ORIGIN",
        thread_id=thread_id,
        delivery_metadata=delivery_metadata,
    )


def test_done_report_is_delivered_into_the_origin_thread(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "done-thread.db"))
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="w watku", assignee="default")
        _subscribe(conn, tid, thread_id="1790440278.131249")
        kb.complete_task(conn, tid, summary="pelny raport z pracy")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 1
    sent = adapter.sent[0]
    assert sent["chat_id"] == "C_ORIGIN"
    assert sent["metadata"]["thread_id"] == "1790440278.131249"
    # No broadcast back into the channel, and no duplicate root send.
    assert sent["metadata"].get("reply_broadcast") in (None, False)
    assert "pelny raport z pracy" in sent["text"]


def test_blocked_report_is_delivered_into_the_origin_thread(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "blocked-thread.db"))
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="blokada", assignee="default")
        _subscribe(conn, tid, thread_id="1790440278.131249")
        kb.block_task(conn, tid, reason="brak dostepu do repo", kind="capability")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 1
    assert adapter.sent[0]["metadata"]["thread_id"] == "1790440278.131249"
    assert "blocked" in adapter.sent[0]["text"]


def test_review_report_is_delivered_into_the_origin_thread(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "review-thread.db"))
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="do review", assignee="default")
        _subscribe(conn, tid, thread_id="1790440278.131249")
        kb.request_review(conn, tid, summary="PR gotowy, wdrozenie po PASS")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 1
    assert adapter.sent[0]["metadata"]["thread_id"] == "1790440278.131249"
    assert adapter.sent[0]["chat_id"] == "C_ORIGIN"


def test_report_is_withheld_when_no_origin_thread_was_recorded(
    tmp_path, monkeypatch
):
    """No anchor => nothing is posted, and the event survives for a retry."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "no-origin.db"))
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="bez origin", assignee="default")
        _subscribe(conn, tid, thread_id="")
        kb.complete_task(conn, tid, summary="raport ktory nie moze zginac")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    # Nothing went to Slack at all — no top-level fallback.
    assert adapter.sent == []
    # ...and the report is still pending, so it can be delivered once the
    # mapping is repaired. This is the "nie gubic raportow" requirement.
    assert len(_unseen(tid, "C_ORIGIN")) == 1


def test_system_announcement_subscription_still_posts_top_level(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "system-lane.db"))
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="ogloszenie", assignee="default")
        _subscribe(
            conn,
            tid,
            thread_id="",
            delivery_metadata={_ALLOW_TOP_LEVEL_KEY: True},
        )
        kb.complete_task(conn, tid, summary="restart stacku zakonczony")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 1
    assert adapter.sent[0]["chat_id"] == "C_ORIGIN"
    assert not adapter.sent[0]["metadata"].get("thread_id")


def test_non_slack_platform_is_unaffected(tmp_path, monkeypatch):
    """The gate must not change Telegram/DM behaviour (no regression)."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "telegram-unaffected.db"))
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="telegram", assignee="default")
        kb.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="tg-chat",
        )
        kb.complete_task(conn, tid, summary="done")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert adapter.sent[0]["chat_id"] == "tg-chat"
