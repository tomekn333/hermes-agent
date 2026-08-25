"""Regresja: modele, ktore odrzucaja temperature/top_p/top_k.

2026-08-25: lista obejmowala tylko Opus 4.7, wiec `vision_analyze` na
claude-opus-5 dostawalo HTTP 400 "`temperature` is deprecated for this model".
Opus-5 jest delegacja i fallbackiem w calym Hermesie, wiec kazde wywolanie
pomocnicze z jawna temperatura sie wywracalo.
"""
import pytest

from agent.anthropic_adapter import _forbids_sampling_params


@pytest.mark.parametrize("model", [
    "claude-opus-4-7",
    "claude-opus-4.7",
    "claude-opus-4-8",
    "claude-opus-5",
    "claude-opus-5[1m]",
    "claude-sonnet-5",
    "claude-haiku-5",
    "claude-fable-5",
])
def test_modele_bez_parametrow_samplingu(model):
    assert _forbids_sampling_params(model) is True


@pytest.mark.parametrize("model", [
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-sonnet-4-5",
    "claude-haiku-4-5",
    "gpt-5.6-sol",
    "kimi-k2",
])
def test_modele_ktore_nadal_przyjmuja_temperature(model):
    assert _forbids_sampling_params(model) is False


def test_kwargs_traca_sampling_dla_opus5():
    """Klucze musza zniknac calkowicie, nie zostac wyzerowane."""
    kwargs = {"model": "claude-opus-5", "temperature": 0.1, "top_p": 0.9, "top_k": 5}
    if _forbids_sampling_params(kwargs["model"]):
        for k in ("temperature", "top_p", "top_k"):
            kwargs.pop(k, None)
    assert "temperature" not in kwargs
    assert "top_p" not in kwargs
    assert "top_k" not in kwargs
