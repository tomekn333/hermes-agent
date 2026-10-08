"""Lokalny patch 2026-10-08: limit rund review per watek karty."""
from agent import kanban_review_guard as g


def test_guard_counts_lineage_and_refuses_over_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "calendar")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_aaaaaaaa")
    monkeypatch.setattr(g, "_task_title", lambda tid: "Rozstrzygnij los sondy")
    rv = ["Wydaj werdykt PASS/FAIL dla SHA abc"]
    assert g.check_review_spawn(rv, cap=3) is None
    assert g.check_review_spawn(rv, cap=3) is None
    # karta naprawcza dziedziczy licznik zrodla
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_bbbbbbbb")
    monkeypatch.setattr(g, "_task_title", lambda tid: "Poprawki po review t_aaaaaaaa (proba 1/2): x")
    assert g.check_review_spawn(rv, cap=3) is None
    msg = g.check_review_spawn(rv, cap=3)
    assert msg and "LIMIT RUND REVIEW" in msg and "calendar:t_aaaaaaaa" in msg
    # zwykla delegacja nie jest liczona ani blokowana
    assert g.check_review_spawn(["Zaimplementuj skeleton"], cap=3) is None


def test_guard_inactive_outside_kanban(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    for _ in range(10):
        assert g.check_review_spawn(["niezalezne review kodu"], cap=1) is None
