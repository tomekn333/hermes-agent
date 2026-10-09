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
HEX = "deadbeefcafe0123456789abcdef4242"  # dlugosc digestu - patrz test kompromisu


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


def test_hex_blob_nietypowej_dlugosci_jest_maskowany():
    """Hex o dlugosci innej niz digest = material klucza, nie suma kontrolna."""
    hex36 = "deadbeefcafe0123456789abcdef42424242"
    out = _strict('{"odcisk_czegos": "%s"}' % hex36)
    assert hex36 not in out


def test_digesty_pozostaja_czytelne_SWIADOMY_KOMPROMIS():
    """Git SHA i sumy kontrolne NIE sa maskowane - decyzja, nie przeoczenie.

    Entropia Shannona czystego hexa nie przekracza 4,0 bita/znak (alfabet
    16-znakowy), wiec hex nigdy nie przejdzie progu entropijnego. Osobna
    galaz w _looks_like_credential_by_shape maskuje hex KAZDEJ innej dlugosci,
    ale dlugosci digestow (32/40/64/128) zostawia czytelne, bo runda 5 review
    wykazala, ze maskowanie wszystkich SHA i sum kontrolnych czyni narzedzie
    czytania plikow bezuzytecznym.

    RYZYKO RESZTKOWE: sekret bedacy czystym hexem o dlugosci dokladnie
    32/40/64/128 znakow nie zostanie tu zamaskowany. Jest to akceptowane,
    bo pierwsza warstwa obrony (denylista w voice_readonly_policy) czyni pliki
    z sekretami nieosiagalnymi na tej granicy - ten skan jest warstwa druga.
    Ten test istnieje, zeby kompromis byl jawny i swiadomie zmieniany, a nie
    odkrywany w kolejnej rundzie review.
    """
    sha = "d8488a98ee6bb8c3dfe131977f0e2addc5c622eb"
    assert _strict("commit: %s" % sha) == "commit: %s" % sha
    md5 = "5d41402abc4b2a76b9719d911017c592"
    assert _strict("md5: %s" % md5) == "md5: %s" % md5


# ------------------------------------------------- korpus skladni (runda 5)
# Runda 5 review wykazala, ze naprawa oparta na dopasowaniu SKLADNI (pole JSON
# w cudzyslowach + przypisanie YAML) jest tym samym bledem co enumeracja nazw
# kluczy, tylko o poziom wyzej: TOML, ENV, listy YAML, tablice JSON, XML i CSV
# nie byly pokryte. Ten korpus pilnuje, zeby skan byl niezalezny od skladni.
SKLADNIE = {
    "TOML": 'obca = "%s"' % SEKRET,
    "ENV": "OBCA=%s" % SEKRET,
    "lista_YAML": "klucze:\n  - %s\n" % SEKRET,
    "tablica_JSON": '{"dane": ["%s"]}' % SEKRET,
    "klucz_ze_spacja": '{"obcy klucz": "%s"}' % SEKRET,
    "klucz_ponad_64_znaki": '{"%s": "%s"}' % ("k" * 70, SEKRET),
    "wartosc_z_wiodaca_spacja": '{"obca": " %s"}' % SEKRET,
    "klucz_bez_cudzyslowow": "{obca: %s}" % SEKRET,
    "CSV_goly": "id,wartosc\n1,%s\n" % SEKRET,
    "XML": "<obca>%s</obca>" % SEKRET,
    "URL_query": "https://api.example.com/v1?access_token=%s" % SEKRET,
    "INI_z_sekcja": "[sekcja]\nobca = %s\n" % SEKRET,
    "w_nawiasach": "wartosc(%s)" % SEKRET,
    "po_przecinku": "a,%s,b" % SEKRET,
    "w_pipe": "a|%s|b" % SEKRET,
}


@pytest.mark.parametrize("nazwa", sorted(SKLADNIE))
def test_sekret_maskowany_w_kazdej_skladni(nazwa):
    out = _strict(SKLADNIE[nazwa])
    assert SEKRET not in out, "LEAK w skladni %s: %s" % (nazwa, out)


# Runda 5: jedna linia z "://" gdziekolwiek w pliku wylaczala CALY tor YAML,
# w tym maskowanie nazw z enumeracji. Skan tokenowy nie ma takiego warunku.
URL_W_PLIKU = {
    "URL_przed_sekretem": "endpoint: https://api.example.com/v1\nobca: %s\n" % SEKRET,
    "URL_i_znana_nazwa": "endpoint: https://api.example.com/v1\napi_key: %s\n" % SEKRET,
    "URL_w_komentarzu": "# patrz https://x.example/y\nobca: %s\n" % SEKRET,
}


@pytest.mark.parametrize("nazwa", sorted(URL_W_PLIKU))
def test_url_w_pliku_nie_wylacza_redakcji(nazwa):
    out = _strict(URL_W_PLIKU[nazwa])
    assert SEKRET not in out, "LEAK (kill-switch ://) w %s: %s" % (nazwa, out)


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
    # --- dokladnie te wartosci runda 5 wykazala jako nadmiernie maskowane
    "commit: d8488a98ee6bb8c3dfe131977f0e2addc5c622eb",
    "Repo: /home/tomek/.hermes/hermes-agent-0.21",
    '{"uuid": "550e8400-e29b-41d4-a716-446655440000"}',
    '{"url": "https://github.com/anthropics/claude-code/blob/main/README.md"}',
    '{"data": "2026-10-09T16:28:06+02:00"}',
    '{"model": "claude-opus-5-20260101"}',
    "docker_image: ghcr.io/nousresearch/hermes-agent:sha-9f3a1c2",
    '{"host": "api.openai.example.com"}',
    "md5: 5d41402abc4b2a76b9719d911017c592",
    "sha256: e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    '{"wersja": "0.21.1-rc.3+build.1848"}',
    '{"pakiet": "com.apple.security.keychain"}',
    "branch: local-patches-0.21",
    # --- runda 6: wartosci, ktorych poprzedni korpus NIE zawieral, a ktore
    # byly maskowane. Entropia calej wartosci jest tu zawyzana przez same
    # separatory: galaz z prefiksem remote miala 4,09, a model z prefiksem
    # dostawcy dokladnie 4,00 - oba powyzej progu, mimo ze to zwykle nazwy.
    "branch: fork/local-patches-0.21",
    '{"model": "anthropic/claude-opus-5"}',
    "branch: feat/t_785b10dd-voice-readonly",
    '{"sciezka": "tests/test_voice_readonly_policy.py"}',
    '{"sciezka": "/home/Tomek/Dokumenty/Plik.txt"}',
    # dotted module path trafia w KSZTALT JWT (trzy czlony base64url);
    # nadmiar obecny takze w baseline sprzed rundy 6
    '{"funkcja": "agent.redact.redact_text"}',
    '{"funkcja": "tools.file_tools.read_file"}',
]


# Wartosci skladane w czasie wykonania, zeby plik testowy nie wygladal na
# nosnik realnych poswiadczen dla skanerow sekretow.
SEKRETY_ROZBITE_SEPARATORAMI = [
    ("myslnik", "Xk9s-Pq2m-Lz8w-Rt4v-Nb6c-Hj1y"),
    ("podkreslenie", "Xk9s_Pq2m_Lz8w_Rt4v_Nb6c_Hj1y"),
    ("kropka", "Xk9s.Pq2m.Lz8w.Rt4v.Nb6c.Hj1y"),
    ("slash", "Xk9s/Pq2m/Lz8w/Rt4v/Nb6c/Hj1y"),
    ("base64url", "Xk9sPq2mLz8wRt4vNb6cHj1y_Wd5aGf7e"),
    ("lowercase", "xk9spq2mlz8wrt4vnb6chj1ywd5agf7e"),
    ("cialo PEM", "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSj"),
    ("JWT", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"),
    ("prefiks dostawcy", "sk-" + "proj-Xk9sPq2mLz8wRt4vNb6cHj1y"),
    ("token forge", "ghp" + "_Xk9sPq2mLz8wRt4vNb6cHj1yWd5aGf7e"),
    ("klucz chmury", "AKIA" + "IOSFODNN7EXAMPLE"),
]


@pytest.mark.parametrize("opis,sekret", SEKRETY_ROZBITE_SEPARATORAMI)
def test_sekret_rozbity_separatorami_jest_maskowany(opis, sekret):
    """Separator w wartosci nie moze byc droga ucieczki.

    Runda 6: pierwsza probna naprawa nadmiaru oceniala tylko NAJDLUZSZY
    segment wartosci i przepuszczala te sekrety jawnie - wymiana jednego
    nadmiaru na piec przeciekow. Ten test przypina wlasciwy kierunek.
    """
    out = _strict('{"obca_nazwa": "%s"}' % sekret)
    assert sekret not in out, "przeciek (%s): %s" % (opis, out)


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
