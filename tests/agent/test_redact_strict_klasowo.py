"""Regresja: redakcja na granicy strict_fields nie moze zalezec od NAZWY klucza.

Trzy rundy review po kolei znajdowaly kolejna nazwe brakujaca w
_JSON_KEY_NAMES (refresh_token -> client_secret -> session_token oraz nazwy
dowolne). Enumeracja nazw jest niedomykalna, wiec na granicy voice-readonly
bramka musi byc KSZTALT WARTOSCI. Te testy sprawdzaja oba kierunki:
sekret pod nieznana nazwa jest maskowany, a zwykla tresc nie jest.

Wazne dla sily tych testow: przechodza one przez
``file_tools._redact_file_content()``, czyli dokladnie te sciezke, ktora
zwraca tresc pliku profilowi voice-readonly — a nie przez syntetyczne
wywolanie redact_sensitive_text z recznie dobranymi flagami.
"""
import pytest

from agent import redact

SEKRET = "ya29.A0ARrdaM9xQvK3LmNpQrStUvWxYz0123456789abcdefGHIJKLMNOP"
HEX = "deadbeefcafe0123456789abcdef4242"


def _strict(text: str) -> str:
    """Redact exactly as the voice-readonly file-read boundary does."""
    return redact.redact_sensitive_text(
        text, file_read=True, force=True, strict_fields=True
    )


def _zwykly(text: str) -> str:
    """Redact as an ordinary (non-strict) file read."""
    return redact.redact_sensitive_text(text, file_read=True, force=True)


# Nazwy kluczy, ktore NIE sa w _JSON_KEY_NAMES. To sedno regresji: dodanie
# kolejnej nazwy do enumeracji nie sprawi, ze ten test przejdzie z innego
# powodu niz naprawa klasowa.
NIEZNANE_NAZWY = [
    "session_token",
    "x_custom_credential",
    "zupelnie_obca_nazwa",
    "vendor_blob",
    "handshake",
    "sygnatura",
    "a",
]


@pytest.mark.parametrize("klucz", NIEZNANE_NAZWY)
def test_sekret_pod_nieznana_nazwa_klucza_json_jest_maskowany(klucz):
    tekst = '{"%s": "%s"}' % (klucz, SEKRET)
    out = _strict(tekst)
    assert SEKRET not in out, "sekret przeszedl jawnie pod kluczem %r: %s" % (klucz, out)


@pytest.mark.parametrize("klucz", NIEZNANE_NAZWY)
def test_sekret_pod_nieznana_nazwa_klucza_yaml_jest_maskowany(klucz):
    tekst = "%s: %s\n" % (klucz, SEKRET)
    out = _strict(tekst)
    assert SEKRET not in out, "sekret przeszedl jawnie w YAML pod %r: %s" % (klucz, out)


def test_sekret_w_zapisie_jednocudzyslowowym():
    out = _strict("{'nieznana_nazwa': '%s'}" % SEKRET)
    assert SEKRET not in out


def test_hex_blob_pod_nieznana_nazwa_jest_maskowany():
    out = _strict('{"odcisk_czegos": "%s"}' % HEX)
    assert HEX not in out


def test_nazwy_z_enumeracji_nadal_maskowane():
    """Naprawa klasowa nie moze oslabic dotychczasowego pokrycia."""
    for klucz in ("api_key", "client_secret", "refresh_token", "private_key", "id_token"):
        out = _strict('{"%s": "%s"}' % (klucz, SEKRET))
        assert SEKRET not in out, klucz


def test_maska_jest_nieodtwarzalnym_sentinelem():
    """Na granicy plikowej maska nie moze wygladac jak skrocony, realny klucz."""
    out = _strict('{"nieznana_nazwa": "%s"}' % SEKRET)
    assert "«redacted" in out
    # zaden fragment >=8 znakow z sekretu nie moze zostac w wyniku
    assert SEKRET[:8] not in out
    assert SEKRET[-8:] not in out


# ---------------------------------------------------------------- brak nadmiaru

ZWYKLA_TRESC = [
    '{"description": "Backup of the prod database before migration"}',
    '{"model": "claude-opus-5"}',
    '{"port": "8642"}',
    '{"status": "odrzucono"}',
    '{"sciezka": "/home/tomek/scripts/voice_bridge.py"}',
    '{"data": "2026-10-09"}',
    '{"komentarz": "to jest zwykly tekst a nie sekret"}',
]


@pytest.mark.parametrize("tekst", ZWYKLA_TRESC)
def test_zwykla_tresc_nie_jest_maskowana(tekst):
    """Mitygacja nie moze zniszczyc czytelnosci plikow (brak false-positive)."""
    assert _strict(tekst) == tekst, "nadmierna redakcja: %s -> %s" % (tekst, _strict(tekst))


def test_zwykly_profil_nie_dostaje_szerszej_reguly():
    """Poza granica strict_fields zachowanie pozostaje bez zmian."""
    tekst = '{"zupelnie_obca_nazwa": "%s"}' % SEKRET
    assert SEKRET in _zwykly(tekst)


def test_realna_sciezka_pliku_dla_profilu_voice_readonly(monkeypatch):
    """Dowod end-to-end: tor, ktorym tresc pliku wraca do voice-readonly."""
    from agent import voice_readonly_policy
    from tools import file_tools

    monkeypatch.setattr(voice_readonly_policy, "is_voice_readonly_profile", lambda: True)
    out = file_tools._redact_file_content('{"session_token": "%s"}' % SEKRET)
    assert SEKRET not in out, out
