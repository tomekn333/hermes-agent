import json
from pathlib import Path

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override


@pytest.fixture
def voice_home(tmp_path):
    home = tmp_path / "profiles" / "voice-readonly"
    home.mkdir(parents=True)
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
    assert result.get("_omitted")
