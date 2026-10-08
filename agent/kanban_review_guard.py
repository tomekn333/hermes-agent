"""Twardy limit rund niezaleznego review per watek zadania Kanban.

Lokalny patch (Tomek, 2026-10-08). Lekcja: karta calendar "sonda Dynamic Type"
przeszla 25 rund review (2 karty "Poprawki po review" + restarty po budzecie),
okolo 200 mln tokenow Claude Max. Limit "3 rundy" z WORKER-RULES dzialal tylko
per PR i per sesje — nowe karty i nowe runy liczyly od zera.

Watek zadania = karta zrodlowa + jej karty "Poprawki po review <id> ...".
Licznik zyje w ``$HERMES_HOME/kanban/review_rounds.json`` (przezywa restarty
runow i karty naprawcze). Rundy liczymy przy SPAWNIE subagenta, ktorego cel
wyglada na niezalezne review/werdykt PASS-FAIL.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import time

LINEAGE_REVIEW_CAP = 5
LIMIT_REASON_TAG = "review-limit"

_REVIEW_RE = re.compile(
    r"(niezale\w*\W+(?:\w+\W+){0,2}review|review\s+kodu|code\s+review|"
    r"werdykt\w*\s+PASS|PASS\s*/\s*FAIL|PASS\s+(?:albo|lub|or)\s+FAIL|"
    r"independent\s+review|audyt\w*\s+pokrycia)",
    re.IGNORECASE,
)
_ROOT_RE = re.compile(r"po review\s+(t_[0-9a-f]{6,})", re.IGNORECASE)


def _home() -> str:
    return os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")


def _store_path() -> str:
    return os.path.join(_home(), "kanban", "review_rounds.json")


def looks_like_review(texts) -> bool:
    for t in texts or ():
        if isinstance(t, str) and _REVIEW_RE.search(t):
            return True
    return False


def lineage_root(task_id: str, title: str | None) -> str:
    m = _ROOT_RE.search(title or "")
    return m.group(1) if m else task_id


def _task_title(task_id: str) -> str | None:
    try:
        from hermes_cli import kanban_db as kb
        with kb.connect() as conn:
            t = kb.get_task(conn, task_id)
            return getattr(t, "title", None)
    except Exception:
        return None


def lineage_key(task_id: str, title: str | None = None) -> str:
    board = (os.environ.get("HERMES_KANBAN_BOARD") or "default").strip() or "default"
    if title is None:
        title = _task_title(task_id)
    return f"{board}:{lineage_root(task_id, title)}"


def rounds_for(key: str) -> int:
    try:
        with open(_store_path(), encoding="utf-8") as fh:
            return int((json.load(fh).get(key) or {}).get("rounds", 0))
    except Exception:
        return 0


def _bump(key: str, task_id: str) -> int:
    path = _store_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            data = {}
        rec = data.get(key) or {"rounds": 0, "tasks": []}
        rec["rounds"] = int(rec.get("rounds", 0)) + 1
        if task_id not in rec.setdefault("tasks", []):
            rec["tasks"].append(task_id)
        rec["last_at"] = int(time.time())
        data[key] = rec
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        return rec["rounds"]


def check_review_spawn(texts, cap: int | None = None) -> str | None:
    """None = wolno (runda policzona); str = komunikat odmowy dla workera."""
    task_id = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    if not task_id or not looks_like_review(texts):
        return None
    cap = LINEAGE_REVIEW_CAP if cap is None else int(cap)
    key = lineage_key(task_id)
    done = rounds_for(key)
    if done >= cap:
        return (
            f"LIMIT RUND REVIEW: watek zadania {key} mial juz {done} rund "
            f"niezaleznego review (twardy limit {cap}, liczony lacznie z kartami "
            f"'Poprawki po review' i kolejnymi runami). NIE zlecaj kolejnej rundy. "
            f"Zrob checkpoint (commit + push), a potem zakoncz karte przez "
            f"kanban_block(reason='{LIMIT_REASON_TAG}: <1 zdanie>') i w handoffie "
            f"opisz: co dziala, jakie blokery zostaly, ile rund padlo i jakie "
            f"jest konkretne pytanie do Tomka (np. czy zawezic zakres)."
        )
    _bump(key, task_id)
    return None
