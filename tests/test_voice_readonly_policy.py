import json
import os
from pathlib import Path

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override


@pytest.fixture
def voice_home(tmp_path, monkeypatch):
    home = tmp_path / "profiles" / "voice-readonly"
    home.mkdir(parents=True)
    # Search roots are an allowlist; point the task workspace at tmp_path so
    # the fixtures below are legitimately searchable.
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path))
    token = set_hermes_home_override(home)
    try:
        yield home
    finally:
        reset_hermes_home_override(token)


def test_profile_detection_uses_effective_profile_home_not_prompt_or_profile_env(voice_home, monkeypatch):
    monkeypatch.setenv("HERMES_PROFILE", "default")
    from agent.voice_readonly_policy import is_voice_readonly_profile

    assert is_voice_readonly_profile()


def test_secret_path_policy_resolves_symlinks_and_allows_safe_files(voice_home, tmp_path):
    from agent.voice_readonly_policy import read_path_denial

    safe = tmp_path / "notes.txt"
    safe.write_text("safe")
    secret = tmp_path / ".env"
    secret.write_text("TOKEN=fake")
    alias = tmp_path / "notes-link.txt"
    alias.symlink_to(secret)

    assert read_path_denial(safe) is None
    assert read_path_denial(secret)
    assert read_path_denial(alias)
    assert read_path_denial(Path.home() / ".ssh" / "id_ed25519")
    assert read_path_denial(tmp_path / "auth.json")
    assert read_path_denial(tmp_path / "credentials.yaml")
    assert read_path_denial(tmp_path / "client.pem")


def test_voice_readonly_schema_hides_kanban_create(voice_home, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test")
    import model_tools

    model_tools._clear_tool_defs_cache()
    names = {
        t["function"]["name"]
        for t in model_tools.get_tool_definitions(
            enabled_toolsets=["voice-readonly"], quiet_mode=True
        )
    }
    assert "kanban_complete" in names
    assert "kanban_create" not in names


def test_kanban_create_handler_rejects_voice_readonly(voice_home, monkeypatch):
    from tools.kanban_tools import _handle_create

    monkeypatch.setattr(
        "tools.kanban_tools._connect",
        lambda **_kw: (_ for _ in ()).throw(AssertionError("must reject before DB access")),
    )

    result = json.loads(_handle_create({"title": "escape", "assignee": "default"}))
    assert result.get("success") is False or result.get("error")
    assert "voice-readonly" in json.dumps(result)


def test_file_handlers_reject_secret_paths_for_voice_readonly(voice_home, tmp_path):
    from tools.file_tools import read_file_tool, search_tool

    secret = tmp_path / ".env"
    secret.write_text("TOKEN=fake")
    read_result = json.loads(read_file_tool(str(secret)))
    search_result = json.loads(search_tool("TOKEN", path=str(secret)))
    assert read_result.get("error")
    assert search_result.get("error")


def test_search_filters_nested_secret_results_for_voice_readonly(voice_home, tmp_path):
    from tools.file_tools import search_tool

    safe = tmp_path / "notes.txt"
    safe.write_text("needle safe")
    secret = tmp_path / "secrets.json"
    secret.write_text("needle TOKEN=fake")

    result = json.loads(search_tool("needle", path=str(tmp_path), limit=20))
    rendered = json.dumps(result)
    assert "notes.txt" in rendered
    assert ".env" not in rendered


# ---------------------------------------------------------------------------
# Regressions for the independent-review findings (2026-10-09)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        # H1: the house `cp f f.back.<ts>` convention produced hundreds of
        # readable copies of secret files that a basename denylist missed.
        "auth.json.back.20260723_182759",
        "auth.json.bak",
        "auth.json~",
        ".env.backup",
        ".env.save",
        ".claude.json.backup.1784199208859",
        "credentials.json.old",
        "id_ed25519.orig",
        # Secret-bearing names that were simply absent from the denylist.
        "prod.env",
        ".pgpass",
        ".my.cnf",
        ".bash_history",
        "client.ovpn",
        "token.txt",
        "password.txt",
        "refresh_token.json",
    ],
)
def test_backup_and_extra_secret_names_are_denied(voice_home, tmp_path, name):
    """H1 — denial must survive backup tails and cover more secret names."""
    from agent.voice_readonly_policy import read_path_denial

    assert read_path_denial(tmp_path / name), name


def test_ordinary_files_still_readable(voice_home, tmp_path):
    """The hardening above must not deny normal project files."""
    from agent.voice_readonly_policy import read_path_denial

    for name in ("notes.txt", "main.py", "README.md", "pyproject.toml",
                 "backup.txt", "old_notes.md"):
        assert read_path_denial(tmp_path / name) is None, name


@pytest.mark.parametrize(
    "name",
    [
        "config.yaml",
        "config.yaml.back.20260716_124703",
        "config.yaml.bak",
        "auth.json.back.20260723_182759",
    ],
)
def test_backups_of_private_home_config_are_denied(voice_home, name):
    """H1 — backups under a private agent home carry the same tokens.

    `config.yaml` is only secret-bearing *because of where it lives*, so the
    backup-tail normalisation has to feed the private-home rule too.
    """
    from agent.voice_readonly_policy import read_path_denial

    assert read_path_denial(Path.home() / ".hermes" / name), name


def test_broad_search_roots_denied_by_allowlist(voice_home):
    """H2 — `/home` and `$HOME/..` used to slip past a three-path denylist."""
    from agent.voice_readonly_policy import read_path_denial

    for root in ("/", "/home", "/home/tomek", "/home/tomek/..",
                 str(Path.home()), str(Path.home() / ".hermes"), "/etc"):
        assert read_path_denial(root, search=True), root


def test_allowed_search_root_passes(voice_home, tmp_path):
    from agent.voice_readonly_policy import read_path_denial

    assert read_path_denial(tmp_path, search=True) is None
    assert read_path_denial(tmp_path / "sub" / "dir", search=True) is None


def test_omitted_counter_is_not_leaked_to_restricted_profile(voice_home, tmp_path):
    """H2 — the `_omitted` count is a per-query oracle on denied content.

    A restricted caller could binary-search a secret by pattern and read the
    counter, so the response must be indistinguishable from "no matches".
    """
    from tools.file_tools import search_tool

    secret = tmp_path / "secrets.json"
    secret.write_text("NEEDLEVALUE")

    hit = json.loads(search_tool("NEEDLEVALUE", path=str(tmp_path), limit=20))
    miss = json.loads(search_tool("ZZZNOSUCHPATTERN", path=str(tmp_path), limit=20))

    assert not hit.get("_omitted")
    assert hit.get("matches") in (None, [], ())
    # Indistinguishable: the key set must not reveal that something was hidden.
    assert set(hit) - {"_warning"} == set(miss) - {"_warning"}


def test_boundary_is_fail_closed_when_profile_cannot_be_resolved(monkeypatch):
    """M1 — an exception used to silently disable the whole boundary."""
    import agent.voice_readonly_policy as policy

    monkeypatch.setattr(policy, "effective_profile_name", lambda: None)
    assert policy.is_voice_readonly_profile()
    assert policy.read_path_denial(Path.home() / ".ssh" / "id_rsa")


def test_restricted_profile_cannot_comment_on_foreign_task(voice_home, monkeypatch):
    """M2 — comments are injected into the next worker's system prompt."""
    from tools.kanban_tools import _handle_comment

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_own")
    monkeypatch.setattr(
        "tools.kanban_tools._connect",
        lambda **_kw: (_ for _ in ()).throw(AssertionError("must reject before DB access")),
    )
    result = json.loads(_handle_comment({"task_id": "t_foreign", "body": "hi"}))
    assert result.get("error")


def test_restricted_profile_cannot_link_foreign_tasks(voice_home, monkeypatch):
    """L1 — a foreign parent→child edge can gate someone else's card."""
    from tools.kanban_tools import _handle_link

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_own")
    monkeypatch.setattr(
        "tools.kanban_tools._connect",
        lambda **_kw: (_ for _ in ()).throw(AssertionError("must reject before DB access")),
    )
    result = json.loads(
        _handle_link({"parent_id": "t_a", "child_id": "t_b"})
    )
    assert result.get("error")


def test_file_content_redaction_survives_global_redaction_switch(voice_home, tmp_path, monkeypatch):
    """H1/L3 — prefix-less secrets in config/data must still be masked.

    `file_read=True` implied `code_file=True`, which disabled the ENV and
    JSON-field patterns, so a `"refresh_token": "<211 chars>"` in a backup of
    auth.json was returned verbatim.  The boundary also must not depend on the
    user's global logging preference.
    """
    import agent.redact as redact
    from tools.file_tools import _redact_file_content

    monkeypatch.setattr(redact, "_REDACT_ENABLED", False)
    secret = "a" * 180
    text = json.dumps({"refresh_token": secret, "note": "ok"})
    out = _redact_file_content(text)
    assert secret not in out


# ---------------------------------------------------------------------------
# Round-2 review findings (2026-10-09)
# ---------------------------------------------------------------------------


def test_total_count_is_not_an_oracle_on_denied_content(voice_home, tmp_path):
    """N1 (HIGH) — hiding `_omitted` left `total_count` leaking the same bit.

    `total_count - len(matches)` reproduced the counter exactly, so a pattern
    binary-search over a denied file still worked.
    """
    from tools.file_tools import search_tool

    secret = tmp_path / "secrets.json"
    secret.write_text("NEEDLEVALUE\nNEEDLEVALUE\n")

    hit = json.loads(search_tool("NEEDLEVALUE", path=str(tmp_path), limit=20))
    miss = json.loads(search_tool("ZZZNOSUCHPATTERN", path=str(tmp_path), limit=20))
    assert hit.get("total_count", 0) == miss.get("total_count", 0) == 0
    assert not hit.get("total_count_is_lower_bound")


def test_truncation_metadata_is_not_an_oracle_on_denied_content(voice_home, tmp_path):
    """R3 N1 — pagination must not distinguish a hidden hit from a miss.

    A context search over a one-result page is the reachable form of the
    oracle: ripgrep fetches extra context, then the response reports its page
    as truncated even after the denied match itself has been removed.
    """
    from tools.file_tools import search_tool

    (tmp_path / "secrets.json").write_text("NEEDLEVALUE\n")
    hit_raw = search_tool("NEEDLEVALUE", path=str(tmp_path), limit=1, context=1)
    miss_raw = search_tool(
        "ZZZNOSUCHPATTERN", path=str(tmp_path), limit=1, context=1
    )

    assert "[Hint: Results truncated." not in hit_raw
    hit = json.loads(hit_raw)
    miss = json.loads(miss_raw)
    for result in (hit, miss):
        assert not result.get("truncated")
        assert not result.get("total_count_is_lower_bound")
        assert result.get("limit_reason") is None
    assert set(hit) - {"_warning"} == set(miss) - {"_warning"}


def test_filtered_total_uses_full_count_not_page_length(voice_home, tmp_path):
    """R3 N5 — a filtered page cannot redefine the full result count."""
    from tools.file_operations import SearchMatch, SearchResult
    from tools.file_tools import _filter_read_blocked_search_results

    denied_path = tmp_path / "secrets.json"
    result = SearchResult(
        matches=[SearchMatch(str(denied_path), 1, "NEEDLEVALUE")],
        total_count=101,
        truncated=True,
        limit_reason="result limit",
    )

    assert _filter_read_blocked_search_results(result) == 1
    assert result.total_count == 100
    assert result.truncated is False
    assert result.limit_reason is None


def test_count_mode_does_not_leak_denied_totals(voice_home, tmp_path):
    """N1 — `count` output mode summed over unfiltered files."""
    from tools.file_tools import search_tool

    (tmp_path / "secrets.json").write_text("NEEDLEVALUE\nNEEDLEVALUE\n")
    out = json.loads(
        search_tool("NEEDLEVALUE", path=str(tmp_path), output_mode="count", limit=20)
    )
    assert out.get("total_count", 0) == 0
    assert not out.get("counts")


def test_json_client_secret_is_redacted(voice_home):
    """N2 — `"client_secret"` is exactly the shape Google's auth.json uses."""
    from tools.file_tools import _redact_file_content

    for key in ("client_secret", "client_id", "private_key_id",
                "signing_secret", "webhook_secret", "session_key"):
        secret = "GOCSPX-" + "b" * 40
        out = _redact_file_content(json.dumps({key: secret}))
        assert secret not in out, key


def test_restricted_profile_cannot_gate_foreign_child(voice_home, monkeypatch):
    """N4 — `parent_id=own, child_id=foreign` slipped past the `and` guard.

    link_tasks degrades the foreign card ready→todo and gates it on ours, so
    the check has to be unconditional on `child_id`.
    """
    from tools.kanban_tools import _handle_link

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_own")
    monkeypatch.setattr(
        "tools.kanban_tools._connect",
        lambda **_kw: (_ for _ in ()).throw(AssertionError("must reject before DB access")),
    )
    result = json.loads(_handle_link({"parent_id": "t_own", "child_id": "t_foreign"}))
    assert result.get("error")


def test_restricted_profile_cannot_link_foreign_parent_to_own_child(voice_home, monkeypatch):
    """R3 N7 — parent subscriptions must not be inherited cross-task."""
    from tools.kanban_tools import _handle_link

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_own")
    monkeypatch.setattr(
        "tools.kanban_tools._connect",
        lambda **_kw: (_ for _ in ()).throw(AssertionError("must reject before DB access")),
    )
    result = json.loads(_handle_link({"parent_id": "t_foreign", "child_id": "t_own"}))
    assert result.get("error")


@pytest.mark.parametrize(
    ("handler_name", "args"),
    [
        ("_handle_complete", {"task_id": "t_foreign", "summary": "done"}),
        ("_handle_block", {"task_id": "t_foreign", "reason": "waiting"}),
        ("_handle_request_review", {"task_id": "t_foreign", "summary": "ready"}),
        ("_handle_request_changes", {"task_id": "t_foreign", "reason": "fix"}),
        ("_handle_heartbeat", {"task_id": "t_foreign"}),
        ("_handle_comment", {"task_id": "t_foreign", "body": "handoff"}),
        (
            "_handle_attach",
            {
                "task_id": "t_foreign",
                "filename": "evidence.txt",
                "content_base64": "eA==",
            },
        ),
        (
            "_handle_attach_url",
            {"task_id": "t_foreign", "url": "https://example.test/evidence.txt"},
        ),
        ("_handle_unblock", {"task_id": "t_foreign"}),
        ("_handle_link", {"parent_id": "t_own", "child_id": "t_foreign"}),
    ],
)
@pytest.mark.parametrize("task_env", ["t_own", None])
def test_restricted_writers_fail_closed_before_database(
    voice_home, monkeypatch, handler_name, args, task_env
):
    """R3 N2 — no writer may become an orchestrator without a task scope."""
    from tools import kanban_tools as kt

    if task_env is None:
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    else:
        monkeypatch.setenv("HERMES_KANBAN_TASK", task_env)
    monkeypatch.setattr(
        kt,
        "_connect",
        lambda **_kw: (_ for _ in ()).throw(AssertionError("must reject before DB access")),
    )

    result = json.loads(getattr(kt, handler_name)(args))
    assert result.get("error"), (handler_name, task_env, result)


def test_restricted_scope_keeps_normal_profile_orchestrator_behavior(monkeypatch):
    """R3 N2 — the additional gate is a no-op outside the restricted tier."""
    from agent import voice_readonly_policy
    from tools.kanban_tools import _enforce_restricted_task_scope

    monkeypatch.setattr(voice_readonly_policy, "is_voice_readonly_profile", lambda: False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert _enforce_restricted_task_scope("t_any", "kanban_complete") is None


@pytest.mark.parametrize("name", ["auth.json~1", "auth.json~", ".env~3",
                                  "id_rsa~12", "auth.json.~1~"])
def test_tilde_numeric_backup_tails_denied(voice_home, tmp_path, name):
    """N5 — `~<digits>` was not part of the backup-tail alternation."""
    from agent.voice_readonly_policy import read_path_denial

    assert read_path_denial(tmp_path / name), name


def test_kanban_create_import_failure_is_controlled(voice_home, monkeypatch):
    """N7 — an ImportError must be a tool_error, not a raw traceback."""
    import builtins

    real_import = builtins.__import__

    def boom(name, *a, **kw):
        if name == "agent.voice_readonly_policy":
            raise ImportError("simulated")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", boom)
    from tools.kanban_tools import _handle_create

    result = json.loads(_handle_create({"title": "x", "assignee": "default"}))
    assert result.get("error")
