"""Evidence-based progress reports for Kanban workers (card t_d283fa6a).

The owner asked for short Polish progress notes in the *origin request
thread*: what was actually finished, that work continues, and what the
worker is doing right now. The hard constraints that shape this module:

* **Evidence only.** A report is emitted for a real git commit that exists
  in the worker's workspace, or for an explicitly declared stage. Nothing
  is inferred — never "it is probably coding now" from a PID or from the
  previous commit's subject.
* **No LLM.** Commit detection is ``git log``; stage text comes from the
  worker's own declaration. There is no periodic model call.
* **No replay.** The first observation of a run records a *baseline* event
  (``progress_baseline``, invisible to the notifier) instead of reporting
  the repository's whole history. The cursor lives in the board DB, so a
  gateway restart cannot re-deliver anything.
* **Delivery is not our business.** We only append ``progress`` events.
  The existing notifier (``gateway/kanban_watchers.py``) owns thread
  routing, the origin-thread gate from card t_08050af3, per-subscription
  dedupe via the claim cursor, rate-limit rewind/backoff and drop rules.
  Writing an event is therefore *not* proof of delivery, by design.

Everything below the ``ProgressSettings`` dataclass is a pure function of
its arguments (clock and subprocess injected) so the behaviour is testable
without a gateway, a git daemon or a Slack workspace.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger("gateway.run")

#: Event kind delivered to subscribers (added to the notifier's claim set).
PROGRESS_EVENT = "progress"

#: Event kind used purely as a cursor/baseline. Deliberately NOT in the
#: notifier's kind list, so recording where a run started emits no message.
BASELINE_EVENT = "progress_baseline"

#: Statuses for which a progress report must never be produced. ``done`` and
#: ``archived`` are finished work (the completion event already reported it);
#: ``blocked`` has its own, louder notification.
_SILENT_STATUSES = frozenset({"done", "archived", "blocked", "triage"})

#: Reported work states. Kept as literal Polish strings because they are
#: user-visible and the owner asked for explicit, fixed wording.
STATE_WORKING = "pracuje"
STATE_WAITING_CI = "czeka na CI"
STATE_REVIEW = "review"
STATE_BLOCKED = "zablokowane"
STATE_DONE = "zakonczone"
STATE_NO_PROGRESS = "brak nowego potwierdzonego postepu"

_VALID_STATES = (
    STATE_WORKING,
    STATE_WAITING_CI,
    STATE_REVIEW,
    STATE_BLOCKED,
    STATE_DONE,
    STATE_NO_PROGRESS,
)

#: Text used when the worker declared no current stage. The owner explicitly
#: asked for an honest gap here rather than a guess carried over from the
#: previous commit.
NO_STAGE_TEXT = "brak zadeklarowanego etapu"


@dataclass(frozen=True)
class ProgressSettings:
    """Live knobs, read from ``kanban.progress_reports`` in config.yaml."""

    enabled: bool = True
    #: Watcher tick spacing.
    interval_seconds: int = 60
    #: Quiet stretch after which a single "no confirmed progress" note fires.
    silence_minutes: int = 10
    #: Minimum spacing between two progress events for the SAME task —
    #: coalesces a burst of commits into one report.
    min_interval_seconds: int = 120
    #: How many commit subjects to name before summarising the remainder.
    max_commits_per_report: int = 3
    #: A declared stage older than this is treated as unknown, so a stale
    #: declaration cannot masquerade as "what the worker is doing now".
    stage_max_age_minutes: int = 30

    @staticmethod
    def from_config(cfg: Any) -> "ProgressSettings":
        """Build settings from a loaded config mapping, failing safe.

        Any malformed value falls back to its default rather than raising:
        a typo in config.yaml must not take the gateway watcher down.
        """
        section: dict = {}
        if isinstance(cfg, dict):
            kanban = cfg.get("kanban")
            if isinstance(kanban, dict):
                raw = kanban.get("progress_reports")
                if isinstance(raw, dict):
                    section = raw
        d = ProgressSettings()

        def _int(key: str, fallback: int, minimum: int) -> int:
            try:
                value = int(section.get(key, fallback))
            except (TypeError, ValueError):
                return fallback
            return max(minimum, value)

        return ProgressSettings(
            enabled=bool(section.get("enabled", d.enabled)),
            interval_seconds=_int("interval_seconds", d.interval_seconds, 5),
            silence_minutes=_int("silence_minutes", d.silence_minutes, 1),
            min_interval_seconds=_int(
                "min_interval_seconds", d.min_interval_seconds, 0
            ),
            max_commits_per_report=_int(
                "max_commits_per_report", d.max_commits_per_report, 1
            ),
            stage_max_age_minutes=_int(
                "stage_max_age_minutes", d.stage_max_age_minutes, 1
            ),
        )


# --------------------------------------------------------------------------
# git evidence
# --------------------------------------------------------------------------


def _run_git(args: list, cwd: str, runner: Optional[Callable] = None) -> Optional[str]:
    """Run a read-only git command, returning stdout or ``None`` on failure.

    Never raises: a missing repo, a detached worktree or a git binary that
    isn't installed must degrade to "no evidence", not to a crashed watcher.
    """
    run = runner or subprocess.run
    try:
        proc = run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("kanban progress: git %s failed in %s: %s", args, cwd, exc)
        return None
    if getattr(proc, "returncode", 1) != 0:
        return None
    return (getattr(proc, "stdout", "") or "").strip()


def head_sha(
    repo_path: Optional[str], runner: Optional[Callable] = None
) -> Optional[str]:
    """Current HEAD sha of *repo_path*, or ``None`` when it is not a repo."""
    if not repo_path or not Path(repo_path).is_dir():
        return None
    return _run_git(["rev-parse", "HEAD"], repo_path, runner) or None


def commits_since(
    repo_path: Optional[str],
    since_sha: Optional[str],
    *,
    limit: int = 20,
    runner: Optional[Callable] = None,
) -> list:
    """Commits reachable from HEAD but not from *since_sha*, oldest first.

    Returns ``[{"sha": <short>, "subject": <str>}, ...]``. An unknown
    ``since_sha`` (rebased or force-moved branch) yields an empty list
    rather than the whole history — replaying old work is worse than
    reporting nothing, and the caller re-baselines on the next tick.
    """
    if not repo_path or not Path(repo_path).is_dir():
        return []
    if not since_sha:
        return []
    # Cheap existence probe first; `A..B` against a missing A errors out and
    # some git versions then print to stdout, which we must not parse.
    if _run_git(["cat-file", "-e", f"{since_sha}^{{commit}}"], repo_path, runner) is None:
        return []
    raw = _run_git(
        ["log", f"--max-count={int(limit)}", "--format=%h%x1f%s", f"{since_sha}..HEAD"],
        repo_path,
        runner,
    )
    if not raw:
        return []
    out = []
    for line in raw.splitlines():
        if "\x1f" not in line:
            continue
        sha, subject = line.split("\x1f", 1)
        sha = sha.strip()
        if sha:
            out.append({"sha": sha, "subject": subject.strip()[:160]})
    out.reverse()  # git log is newest-first; report chronologically
    return out


# --------------------------------------------------------------------------
# declared stage ("Teraz: ...")
# --------------------------------------------------------------------------


def declared_stage(
    events: Iterable,
    *,
    now: int,
    max_age_minutes: int,
    run_id: Optional[int] = None,
) -> Optional[str]:
    """The worker's own statement of what it is doing right now.

    Sources, newest wins: a ``heartbeat`` event carrying a ``note``, or a
    ``progress_stage`` event carrying ``stage``. Anything older than
    *max_age_minutes* is discarded — the requirement is explicit that a
    stale declaration must not be presented as the current stage, and that
    the stage is never inferred from the previous commit.

    Returns ``None`` when no fresh declaration exists; the caller renders
    :data:`NO_STAGE_TEXT` rather than inventing one.
    """
    cutoff = now - max_age_minutes * 60
    best_at = -1
    best_text: Optional[str] = None
    for ev in events:
        created = int(getattr(ev, "created_at", 0) or 0)
        if created < cutoff or created < best_at:
            continue
        ev_run = getattr(ev, "run_id", None)
        if run_id is not None and ev_run is not None and int(ev_run) != int(run_id):
            continue
        payload = getattr(ev, "payload", None)
        if not isinstance(payload, dict):
            continue
        kind = getattr(ev, "kind", "")
        text = None
        if kind == "heartbeat":
            text = payload.get("note")
        elif kind == "progress_stage":
            text = payload.get("stage")
        if not text:
            continue
        text = " ".join(str(text).split())
        if not text:
            continue
        best_at = created
        best_text = text[:160]
    return best_text


def stage_from_workspace(
    workspace_path: Optional[str],
    *,
    now: int,
    max_age_minutes: int,
) -> Optional[str]:
    """Stage declared by writing ``.hermes/stage.txt`` inside the workspace.

    A file-based lane exists so a worker can declare its stage without
    spending a tool call, and so non-agent processes (scripts, CI helpers)
    can participate. Same freshness rule as the event lane.
    """
    if not workspace_path:
        return None
    path = Path(workspace_path) / ".hermes" / "stage.txt"
    try:
        stat = path.stat()
        if stat.st_mtime < now - max_age_minutes * 60:
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    text = " ".join(text.split())
    return text[:160] or None


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def render_progress_message(payload: dict) -> str:
    """Render one progress event payload as the short Polish report.

    Shape asked for by the owner::

        Zapisano <sha>: <krotko co zrobiono>. Praca trwa. Teraz: <etap>

    with an explicit state word and, when nothing verifiable happened, an
    honest "no confirmed progress" instead of a fabricated activity.
    """
    state = str(payload.get("state") or STATE_WORKING)
    commits = payload.get("commits") or []
    stage = payload.get("stage")
    omitted = int(payload.get("commits_omitted") or 0)

    parts = []
    if commits:
        head = commits[0]
        first = f"Zapisano {head.get('sha')}: {head.get('subject')}"
        rest = [f"{c.get('sha')}: {c.get('subject')}" for c in commits[1:]]
        if rest:
            first += "; " + "; ".join(rest)
        if omitted > 0:
            first += f" (+{omitted} dalszych commitow)"
        parts.append(first.rstrip(".") + ".")
    else:
        parts.append("Brak nowego commita.")

    if state == STATE_NO_PROGRESS:
        parts.append("Brak nowego potwierdzonego postepu.")
    elif state == STATE_WORKING:
        parts.append("Praca trwa.")
    else:
        parts.append(f"Stan: {state}.")

    parts.append(f"Teraz: {stage or NO_STAGE_TEXT}")
    return " ".join(parts)


# --------------------------------------------------------------------------
# decision logic
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ProgressDecision:
    """What the watcher should do for one task this tick."""

    action: str  # "skip" | "baseline" | "report"
    payload: Optional[dict] = None
    baseline_sha: Optional[str] = None
    reason: str = ""


def _last_progress_row(events: Iterable, run_id: Optional[int]) -> Optional[Any]:
    """Newest ``progress``/``progress_baseline`` event for this run."""
    best = None
    best_at = -1
    for ev in events:
        if getattr(ev, "kind", "") not in (PROGRESS_EVENT, BASELINE_EVENT):
            continue
        ev_run = getattr(ev, "run_id", None)
        if run_id is not None and ev_run is not None and int(ev_run) != int(run_id):
            continue
        created = int(getattr(ev, "created_at", 0) or 0)
        if created >= best_at:
            best_at = created
            best = ev
    return best


def decide_for_task(
    *,
    task: Any,
    events: Iterable,
    settings: ProgressSettings,
    now: int,
    git_runner: Optional[Callable] = None,
) -> ProgressDecision:
    """Pure decision for a single task: skip, baseline, or report what.

    The caller does the writing; keeping the decision pure is what makes
    dedupe, throttling, silence and restart behaviour testable without a
    board, a gateway or a real repository.
    """
    status = str(getattr(task, "status", "") or "")
    if status in _SILENT_STATUSES or status != "running":
        return ProgressDecision("skip", reason=f"status={status or 'unknown'}")

    events = list(events)
    run_id = getattr(task, "current_run_id", None)
    workspace = getattr(task, "workspace_path", None)

    last = _last_progress_row(events, run_id)
    cursor_sha = None
    last_at = 0
    last_was_silence = False
    if last is not None:
        last_at = int(getattr(last, "created_at", 0) or 0)
        payload = getattr(last, "payload", None)
        if isinstance(payload, dict):
            cursor_sha = payload.get("head_sha") or payload.get("sha")
            last_was_silence = (
                str(payload.get("state") or "") == STATE_NO_PROGRESS
            )

    current_head = head_sha(workspace, git_runner) if workspace else None

    # First sight of this run: remember where it started, report nothing.
    # This is what stops a fresh subscription from replaying repo history.
    if last is None:
        if current_head is None:
            return ProgressDecision("skip", reason="no-repo")
        return ProgressDecision(
            "baseline", baseline_sha=current_head, reason="first-observation"
        )

    stage = declared_stage(
        events,
        now=now,
        max_age_minutes=settings.stage_max_age_minutes,
        run_id=run_id,
    ) or stage_from_workspace(
        workspace, now=now, max_age_minutes=settings.stage_max_age_minutes
    )

    new_commits = []
    if current_head and cursor_sha and current_head != cursor_sha:
        new_commits = commits_since(
            workspace, cursor_sha, limit=50, runner=git_runner
        )

    if new_commits:
        if now - last_at < settings.min_interval_seconds:
            # Coalesce: leave the cursor alone, the next tick reports the
            # whole burst as one message.
            return ProgressDecision("skip", reason="throttled")
        shown = new_commits[-settings.max_commits_per_report:]
        payload = {
            "state": STATE_WORKING,
            "commits": shown,
            "commits_omitted": max(0, len(new_commits) - len(shown)),
            "head_sha": current_head,
            "stage": stage,
            "run_id": run_id,
        }
        return ProgressDecision("report", payload=payload, reason="new-commits")

    # No new commit. One bounded silence note per quiet stretch: if the last
    # thing we said was already "no confirmed progress", stay quiet until
    # real evidence appears.
    if last_was_silence:
        return ProgressDecision("skip", reason="silence-already-reported")
    if now - last_at < settings.silence_minutes * 60:
        return ProgressDecision("skip", reason="silence-window-open")
    payload = {
        "state": STATE_NO_PROGRESS,
        "commits": [],
        "commits_omitted": 0,
        "head_sha": current_head or cursor_sha,
        "stage": stage,
        "run_id": run_id,
    }
    return ProgressDecision("report", payload=payload, reason="silence")


# --------------------------------------------------------------------------
# board write
# --------------------------------------------------------------------------


def apply_decision(conn: Any, task_id: str, decision: ProgressDecision, kb: Any) -> bool:
    """Persist a decision as a board event. Returns True when a row was written.

    Both event kinds go through ``_append_event`` inside a write txn, so the
    cursor and the delivered report are the same durable record — there is no
    second state file that could drift from the board after a restart.
    """
    if decision.action == "skip":
        return False
    with kb.write_txn(conn):
        if decision.action == "baseline":
            kb._append_event(
                conn,
                task_id,
                BASELINE_EVENT,
                {"head_sha": decision.baseline_sha},
                run_id=None,
            )
            return True
        payload = dict(decision.payload or {})
        run_id = payload.pop("run_id", None)
        payload["text"] = render_progress_message(payload)
        kb._append_event(
            conn,
            task_id,
            PROGRESS_EVENT,
            payload,
            run_id=int(run_id) if run_id is not None else None,
        )
    return True


def scan_board(
    conn: Any,
    *,
    kb: Any,
    settings: ProgressSettings,
    now: Optional[int] = None,
    git_runner: Optional[Callable] = None,
) -> dict:
    """Evaluate every running task on one board; return a small tally.

    Only tasks that (a) are ``running`` and (b) have a workspace path are
    candidates, so the per-tick cost is one ``git rev-parse`` per live
    worker — no LLM, no network.
    """
    now = int(now if now is not None else time.time())
    tally = {"checked": 0, "baselined": 0, "reported": 0}
    try:
        tasks = kb.list_tasks(conn, status="running")
    except Exception as exc:
        logger.debug("kanban progress: list_tasks failed: %s", exc)
        return tally
    for task in tasks:
        if not getattr(task, "workspace_path", None):
            continue
        tally["checked"] += 1
        try:
            events = kb.list_events(conn, task.id)
            decision = decide_for_task(
                task=task,
                events=events,
                settings=settings,
                now=now,
                git_runner=git_runner,
            )
            if apply_decision(conn, task.id, decision, kb):
                key = "baselined" if decision.action == "baseline" else "reported"
                tally[key] += 1
        except Exception as exc:
            # Isolate per-task failures: one bad workspace must not stop
            # progress reporting for every other live worker.
            logger.warning(
                "kanban progress: task %s failed: %s", getattr(task, "id", "?"), exc
            )
    return tally


def format_event_for_delivery(payload: Optional[dict]) -> str:
    """Notifier-facing renderer: payload → the line users read.

    Prefers the text frozen at write time so a later wording change cannot
    rewrite history, and re-renders only for rows written before ``text``
    was stored.
    """
    if not isinstance(payload, dict):
        return "Brak nowego potwierdzonego postepu."
    text = payload.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    return render_progress_message(payload)


__all__ = [
    "BASELINE_EVENT",
    "NO_STAGE_TEXT",
    "PROGRESS_EVENT",
    "ProgressDecision",
    "ProgressSettings",
    "STATE_BLOCKED",
    "STATE_DONE",
    "STATE_NO_PROGRESS",
    "STATE_REVIEW",
    "STATE_WAITING_CI",
    "STATE_WORKING",
    "apply_decision",
    "commits_since",
    "decide_for_task",
    "declared_stage",
    "format_event_for_delivery",
    "head_sha",
    "render_progress_message",
    "scan_board",
    "stage_from_workspace",
]
