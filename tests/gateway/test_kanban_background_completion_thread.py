"""Background/async completions must reply inside the origin Slack thread.

Card t_08050af3: technical reports about a task — including the ones that
re-enter a conversation as synthetic turns (``terminal(background=True,
notify_on_complete=True)`` completions, ``delegate_task(background=True)``
completions) — belong in the thread of the original request, not as a new
top-level channel message.

The chain under test is the real one:
``_build_process_event_source`` (which resolves the persisted origin for the
event's session key) -> ``_thread_metadata_for_source`` -> the Slack
adapter's ``_resolve_thread_ts``, which is what finally puts ``thread_ts`` on
the ``chat.postMessage`` call.
"""

from types import SimpleNamespace

from gateway.config import Platform
from gateway.run import GatewayRunner, _parse_session_key, _resolve_progress_thread_id


ORIGIN_THREAD = "1790440278.131249"


def _runner_with_origin(origin):
    """A bare runner whose session store knows one persisted origin."""
    runner = GatewayRunner.__new__(GatewayRunner)
    entry = SimpleNamespace(origin=origin)
    store = SimpleNamespace(
        _ensure_loaded=lambda: None,
        _entries={"agent:main:slack:thread:C_ORIGIN:" + ORIGIN_THREAD: entry},
    )
    runner.session_store = store
    return runner


def _slack_thread_origin():
    from gateway.session import SessionSource

    return SessionSource(
        platform=Platform.SLACK,
        chat_id="C_ORIGIN",
        chat_type="thread",
        thread_id=ORIGIN_THREAD,
        user_id="U0B5A8YPL9W",
    )


def test_background_completion_source_keeps_the_origin_thread():
    origin = _slack_thread_origin()
    runner = _runner_with_origin(origin)
    evt = {
        "session_key": "agent:main:slack:thread:C_ORIGIN:" + ORIGIN_THREAD,
        "type": "background_process",
    }
    source = runner._build_process_event_source(evt)
    assert source is origin
    assert source.thread_id == ORIGIN_THREAD
    assert source.chat_id == "C_ORIGIN"


def test_background_completion_metadata_carries_thread_ts():
    runner = _runner_with_origin(_slack_thread_origin())
    evt = {
        "session_key": "agent:main:slack:thread:C_ORIGIN:" + ORIGIN_THREAD,
        "type": "async_delegation",
    }
    source = runner._build_process_event_source(evt)
    metadata = runner._thread_metadata_for_source(source)
    assert metadata is not None
    assert metadata.get("thread_id") == ORIGIN_THREAD


def test_slack_adapter_anchors_the_send_on_that_thread():
    """The metadata above must actually become ``thread_ts`` on the API call."""
    adapter = object.__new__(_slack_adapter_cls())
    adapter.config = SimpleNamespace(extra={"reply_in_thread": True})
    resolved = adapter._resolve_thread_ts(None, {"thread_id": ORIGIN_THREAD})
    assert resolved == ORIGIN_THREAD


def test_session_key_without_thread_yields_no_anchor():
    """A flat channel session has no thread to reuse — and we do NOT invent one.

    ``_parse_session_key`` deliberately refuses to read the 6th segment as a
    thread for group/channel sessions (it may be a user_id), so a report for
    such a session has no origin anchor. The notifier's withhold gate
    (tests/gateway/test_kanban_notifier_origin_thread.py) is what stops that
    case from becoming a new top-level message.
    """
    parsed = _parse_session_key("agent:main:slack:group:C_ORIGIN:U0B5A8YPL9W")
    assert parsed is not None
    assert "thread_id" not in parsed


def test_progress_bubble_reuses_the_origin_thread_not_a_new_root():
    """Progress/status bubbles anchor on the source thread when there is one."""
    assert (
        _resolve_progress_thread_id(Platform.SLACK, ORIGIN_THREAD, "1790440300.000100")
        == ORIGIN_THREAD
    )


def _slack_adapter_cls():
    from plugins.platforms.slack.adapter import SlackAdapter

    return SlackAdapter
