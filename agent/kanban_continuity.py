"""Ciaglosc pracy workera Kanbana przy wyczerpaniu budzetu iteracji.

Lokalny patch (Tomek, 2026-10-08). Problem: po 200/200 iteracjach run byl
zamykany jako ``timed_out`` z samym bledem — podsumowanie, o ktore agent byl
proszony, przepadalo. Nastepne podejscie zaczynalo rozpoznanie od zera
(38 mln tokenow na jeden run), a po 2 takich runach karta trafiala do
``blocked`` bez informacji, co dalej.

Ten modul daje dwa elementy po stronie workera:

* ``maybe_inject_checkpoint`` — przy ~85% budzetu dokleja do ostatniego wyniku
  narzedzia polecenie: commit + push WIP i przygotowanie handoffu. Doklejenie
  do JESZCZE NIEWYSLANEGO wyniku narzedzia nie lamie naprzemiennosci rol ani
  cache promptu (zmienia sie wylacznie ostatni, niecachowany element).
* ``summary_request`` — przy wyczerpaniu budzetu prosi o ustrukturyzowany
  HANDOFF zamiast ogolnego podsumowania; tekst trafia do ``task_runs.summary``
  (patrz ``kanban_db.record_budget_exhausted``) i nastepny run widzi go jako
  "Prior attempt".
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

CHECKPOINT_FRACTION = 0.85
CHECKPOINT_MARKER = "[KANBAN CHECKPOINT]"

CHECKPOINT_TEXT = (
    CHECKPOINT_MARKER + " Zostalo ok. {remaining} iteracji budzetu tego runu. "
    "Teraz, zanim zrobisz cokolwiek innego: (1) commit + push calej biezacej "
    "pracy na galaz zadania (WIP jest OK), (2) nie zaczynaj nowej rundy review "
    "ani dlugiego testu, (3) jesli zadanie jest prawie skonczone — domknij je "
    "(kanban_complete z handoffem); jesli nie — dokoncz najmniejszy sensowny "
    "krok. Gdy budzet sie skonczy, nastepny run tego zadania wystartuje "
    "automatycznie od Twojego handoffu, jesli w tym runie powstaly commity."
)

HANDOFF_REQUEST = (
    "Budzet iteracji tego runu sie wyczerpal. NIE wywoluj zadnych narzedzi. "
    "Napisz HANDOFF dla nastepnego runu tego samego zadania Kanban (zobaczy go "
    "jako 'Prior attempt' i na jego podstawie bedzie kontynuowal, bez "
    "ponownego rozpoznania). Po polsku, maks. 25 linii, dokladnie te sekcje:\n"
    "ZROBIONE: co jest gotowe i zweryfikowane\n"
    "STAN REPO: galaz, ostatni SHA, czy wypchniete, numer PR\n"
    "ZOSTALO: konkretne kroki do konca zadania\n"
    "NASTEPNY KROK: pierwsza komenda albo plik do ruszenia\n"
    "PULAPKI: co nie dzialalo i dlaczego (zeby nie powtarzac)"
)


def kanban_task_id() -> str | None:
    return (os.environ.get("HERMES_KANBAN_TASK") or "").strip() or None


def checkpoint_due(api_call_count: int, max_iterations: int) -> bool:
    try:
        mx = int(max_iterations)
    except (TypeError, ValueError):
        return False
    if mx < 20:
        return False
    return int(api_call_count) >= int(mx * CHECKPOINT_FRACTION)


def maybe_inject_checkpoint(agent, messages, api_call_count) -> bool:
    """Jednorazowo doklej polecenie checkpointu do ostatniego wyniku narzedzia."""
    try:
        if not kanban_task_id():
            return False
        if getattr(agent, "_kanban_checkpoint_sent", False):
            return False
        if not checkpoint_due(api_call_count, getattr(agent, "max_iterations", 0)):
            return False
        if not messages:
            return False
        last = messages[-1]
        if not isinstance(last, dict) or last.get("role") != "tool":
            return False  # sprobujemy przy nastepnej iteracji
        content = last.get("content")
        if not isinstance(content, str) or CHECKPOINT_MARKER in content:
            return False
        remaining = max(0, int(agent.max_iterations) - int(api_call_count))
        last["content"] = content + "\n\n" + CHECKPOINT_TEXT.format(remaining=remaining)
        agent._kanban_checkpoint_sent = True
        logger.info(
            "kanban checkpoint nudge injected (task=%s, call=%s/%s)",
            kanban_task_id(), api_call_count, agent.max_iterations,
        )
        return True
    except Exception:
        logger.debug("kanban checkpoint nudge failed", exc_info=True)
        return False


def summary_request(default: str) -> str:
    return HANDOFF_REQUEST if kanban_task_id() else default
