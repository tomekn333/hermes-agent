"""Regresje rundy 6 niezaleznego review (t_785b10dd).

Przeciek: krotkie losowe tokeny (20-28 znakow) nie osiagaly stalego progu
entropii 4,0, bo entropia n znakow jest ograniczona przez log2(n). Sekret
zaraz po ``//`` w URL-u przechodzil, bo skan dzieli po ':' i test ``://``
nie widzial schematu.

Nadmiar: identyfikatory sklejone z DLUGICH slow (``internationalization``,
``customerengagementpipeline``) przekraczaly prog entropii.

Oba kierunki + test statystyczny na losowych tokenach, zeby naprawa nie byla
kolejna enumeracja przykladow.
"""
import random
import string

import pytest

from agent import redact


def _strict(text: str) -> str:
    return redact.redact_sensitive_text(
        text, file_read=True, force=True, strict_fields=True
    )


SEKRETY_KROTKIE = [
    "fK9LdBwYNcqB6KrfRtK0",
    "fbpgJ9/Mv7f90QVW7sfc",
    "fbpgJ9_Mv7f90QVW7sfc7s",
    "Qz8xVb3Nm1Lk7Jh5Gf2D",
    "a1B2c3D4e5F6g7H8i9J0k",
]


@pytest.mark.parametrize("sekret", SEKRETY_KROTKIE)
def test_krotki_losowy_token_jest_maskowany(sekret):
    for tekst in ('{"obca": "%s"}' % sekret, "obca = %s" % sekret, "OBCA=%s" % sekret):
        assert sekret not in _strict(tekst), tekst


@pytest.mark.parametrize("sekret", SEKRETY_KROTKIE)
def test_sekret_po_podwojnym_ukosniku_w_url_jest_maskowany(sekret):
    tekst = "endpoint: https://%s@api.example.com/v1" % sekret
    assert sekret not in _strict(tekst), _strict(tekst)
    tekst = "url = https://%s" % sekret
    assert sekret not in _strict(tekst), _strict(tekst)


ZWYKLE = [
    "internationalization",
    "module: company.authenticationmiddleware.jwtvalidator",
    "service = observabilityaggregationservice",
    "pipeline: customerengagementpipeline",
    "image: registry.example.com/platform/customeridentificationplatform:1.2.3",
    "docs: https://customerengagementplatform.example.com/documentation/getting-started",
    "class XMLHttpRequestUpload2",
    "bridge = WhatsAppBridgeOutbound",
    "name: getUserAuthenticationToken",
    "model: claude-opus-5-20260101",
    "branch: fork/local-patches-0.21",
    "plik: voice_bridge.py.back.20261009_095529",
]


@pytest.mark.parametrize("tekst", ZWYKLE)
def test_zwykla_tresc_ze_slow_nie_jest_maskowana(tekst):
    assert _strict(tekst) == tekst, _strict(tekst)


@pytest.mark.parametrize("n", [20, 24, 32])
def test_statystycznie_losowe_base62_sa_maskowane(n):
    """Nie lista przykladow: >=99,5% losowych tokenow base62 musi zniknac."""
    rnd = random.Random(1000 + n)
    alf = string.ascii_letters + string.digits
    proby = 2000
    przecieki = sum(
        not redact._looks_like_credential_by_shape("".join(rnd.choice(alf) for _ in range(n)))
        for _ in range(proby)
    )
    assert przecieki / proby <= 0.005, "przecieki %d/%d przy n=%d" % (przecieki, proby, n)
