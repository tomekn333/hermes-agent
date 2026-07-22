"""OpenAI-compatible shim that forwards Hermes requests to local Claude Code CLI.

This adapter lets Hermes use a user's Claude Max subscription via the
`claude -p --output-format json` subprocess path as a last-resort fallback,
instead of routing Opus traffic through the metered Anthropic Messages API.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

try:  # Reuse the existing text-tool-call parser when available.
    from agent.copilot_acp_client import _extract_tool_calls_from_text
except Exception:  # pragma: no cover - defensive import fallback
    def _extract_tool_calls_from_text(text: str) -> tuple[list[Any], str]:
        return [], text

CLAUDE_CLI_MARKER_BASE_URL = "process://claude-code-cli"
_DEFAULT_TIMEOUT_SECONDS = 900.0


def _resolve_command() -> str:
    return (
        os.getenv("HERMES_CLAUDE_CLI_COMMAND", "").strip()
        or os.getenv("CLAUDE_CLI_PATH", "").strip()
        or shutil.which("claude")
        or "claude"
    )


def _resolve_args() -> list[str]:
    raw = os.getenv("HERMES_CLAUDE_CLI_ARGS", "").strip()
    return shlex.split(raw) if raw else []


def _resolve_home_dir() -> str:
    """Return the real user HOME so Claude Code can find ~/.claude auth."""

    try:
        from hermes_constants import get_subprocess_home

        subprocess_home = get_subprocess_home()
        hermes_home = os.environ.get("HERMES_HOME", "").strip()
        if subprocess_home and not (hermes_home and subprocess_home.startswith(hermes_home)):
            return subprocess_home
    except Exception:
        hermes_home = os.environ.get("HERMES_HOME", "").strip()

    hermes_home = os.environ.get("HERMES_HOME", "").strip()
    home = os.environ.get("HOME", "").strip()
    # Kanban/profile workers often run with HOME=$HERMES_HOME/home for
    # isolation. Claude Code OAuth lives in the real user's ~/.claude, so
    # prefer /home/<user> when HOME is the profile shim.
    if home and not (hermes_home and home.startswith(hermes_home)):
        return home
    try:
        import pwd

        real_home = pwd.getpwuid(os.getuid()).pw_dir.strip()
        if real_home:
            return real_home
    except Exception:
        pass
    expanded = os.path.expanduser("~")
    if expanded and expanded != "~":
        return expanded
    return "/tmp"


def _build_subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    env["HOME"] = _resolve_home_dir()
    return env


def _format_messages_as_prompt(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> str:
    sections: list[str] = [
        "You are being used as the local Claude Code CLI fallback backend for Hermes.",
        "Answer the current request directly. If prior assistant/tool messages are present, treat them as conversation history.",
    ]
    if isinstance(tools, list) and tools:
        sections.append(
            "Hermes supplied tool schemas, but this Claude CLI fallback cannot execute Hermes tools directly. "
            "If a tool call is essential, emit it as <tool_call>{...}</tool_call> JSON in OpenAI function-call shape; otherwise answer normally."
        )

    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "user").upper()
        content = msg.get("content", "")
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") in {"text", "input_text"}:
                        parts.append(str(item.get("text") or ""))
                    elif item.get("type") in {"image_url", "input_image"}:
                        parts.append("[image omitted by Claude CLI fallback]")
                else:
                    parts.append(str(item))
            content_text = "\n".join(p for p in parts if p)
        else:
            content_text = str(content or "")
        if content_text.strip():
            sections.append(f"\n[{role}]\n{content_text.strip()}")
    return "\n".join(sections).strip()


def _normalise_timeout(timeout: Any) -> float:
    if timeout is None:
        return _DEFAULT_TIMEOUT_SECONDS
    if isinstance(timeout, (int, float)):
        return float(timeout)
    candidates = [getattr(timeout, attr, None) for attr in ("read", "write", "connect", "pool", "timeout")]
    numeric = [float(v) for v in candidates if isinstance(v, (int, float))]
    return max(numeric) if numeric else _DEFAULT_TIMEOUT_SECONDS


def _load_claude_json(stdout: str) -> dict[str, Any]:
    text = (stdout or "").strip()
    if not text:
        raise RuntimeError("Claude CLI returned empty stdout")
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            continue
    raise RuntimeError(f"Claude CLI did not return parseable JSON: {text[:500]}")


def _extract_text(data: dict[str, Any]) -> tuple[str, str]:
    if data.get("is_error") is True:
        message = data.get("result") or data.get("message") or data.get("error") or data
        raise RuntimeError(f"Claude CLI reported an error: {message}")

    text = data.get("result") or data.get("response") or data.get("content") or data.get("text") or ""
    if isinstance(text, list):
        pieces: list[str] = []
        for item in text:
            if isinstance(item, dict):
                pieces.append(str(item.get("text") or item.get("content") or ""))
            else:
                pieces.append(str(item))
        text = "\n".join(p for p in pieces if p)
    reasoning = data.get("reasoning") or data.get("thinking") or ""
    return str(text or ""), str(reasoning or "")


class _ClaudeCLIChatCompletions:
    def __init__(self, client: "ClaudeCLIClient"):
        self._client = client

    def create(self, **kwargs: Any) -> Any:
        return self._client._create_chat_completion(**kwargs)


class _ClaudeCLIChatNamespace:
    def __init__(self, client: "ClaudeCLIClient"):
        self.completions = _ClaudeCLIChatCompletions(client)


class ClaudeCLIClient:
    """Minimal OpenAI-client-compatible facade for `claude -p`."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        default_headers: dict[str, str] | None = None,
        command: str | None = None,
        args: list[str] | None = None,
        cwd: str | None = None,
        **_: Any,
    ):
        self.api_key = api_key or "claude-code-cli"
        self.base_url = base_url or CLAUDE_CLI_MARKER_BASE_URL
        self._default_headers = dict(default_headers or {})
        self._command = command or _resolve_command()
        self._args = list(args or _resolve_args())
        self._cwd = str(Path(cwd or os.getcwd()).resolve())
        self.chat = _ClaudeCLIChatNamespace(self)
        self.is_closed = False
        self._active_process: subprocess.Popen[str] | None = None

    def close(self) -> None:
        proc = self._active_process
        self._active_process = None
        self.is_closed = True
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _create_chat_completion(
        self,
        *,
        model: str | None = None,
        messages: list[dict[str, Any]] | None = None,
        timeout: Any = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
        **_: Any,
    ) -> Any:
        del tool_choice  # Claude CLI fallback receives a single formatted prompt.
        prompt_text = _format_messages_as_prompt(messages or [], tools=tools)
        response_text, reasoning_text, usage_data = self._run_prompt(
            prompt_text,
            model=model or "claude-opus-4-8",
            timeout_seconds=_normalise_timeout(timeout),
        )
        tool_calls, cleaned_text = _extract_tool_calls_from_text(response_text)
        usage = SimpleNamespace(
            prompt_tokens=int(usage_data.get("input_tokens") or usage_data.get("prompt_tokens") or 0),
            completion_tokens=int(usage_data.get("output_tokens") or usage_data.get("completion_tokens") or 0),
            total_tokens=int(usage_data.get("total_tokens") or 0),
            prompt_tokens_details=SimpleNamespace(cached_tokens=int(usage_data.get("cache_read_input_tokens") or 0)),
        )
        if not usage.total_tokens:
            usage.total_tokens = usage.prompt_tokens + usage.completion_tokens
        assistant_message = SimpleNamespace(
            content=cleaned_text,
            tool_calls=tool_calls,
            reasoning=reasoning_text or None,
            reasoning_content=reasoning_text or None,
            reasoning_details=None,
        )
        finish_reason = "tool_calls" if tool_calls else "stop"
        return SimpleNamespace(
            choices=[SimpleNamespace(message=assistant_message, finish_reason=finish_reason)],
            usage=usage,
            model=model or "claude-opus-4-8",
        )

    def _run_prompt(self, prompt_text: str, *, model: str, timeout_seconds: float) -> tuple[str, str, dict[str, Any]]:
        cmd = [self._command, "-p", "--model", model, "--output-format", "json", *self._args]
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=self._cwd,
                env=_build_subprocess_env(),
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Could not start Claude CLI command '{self._command}'. "
                "Install Claude Code CLI or set HERMES_CLAUDE_CLI_COMMAND/CLAUDE_CLI_PATH."
            ) from exc

        self.is_closed = False
        self._active_process = proc
        try:
            stdout, stderr = proc.communicate(prompt_text, timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            self.close()
            raise TimeoutError(f"Claude CLI timed out after {timeout_seconds:.0f}s") from exc
        finally:
            if self._active_process is proc:
                self._active_process = None

        if proc.returncode != 0:
            err = (stderr or stdout or "").strip()
            raise RuntimeError(f"Claude CLI exited with status {proc.returncode}: {err[:1000]}")

        data = _load_claude_json(stdout)
        text, reasoning = _extract_text(data)
        raw_usage = data.get("usage")
        usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
        return text, reasoning, usage
