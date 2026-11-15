# ruff: noqa: E501
"""TOTP as RFC 6238 defines it: the published test vectors, the window, and a code never accepted twice."""
import base64

from venthora import totp

RFC_SECRET = base64.b32encode(b"12345678901234567890").decode()  # the RFC's SHA-1 key


def test_the_rfc_6238_vectors():
    # RFC 6238 appendix B, SHA-1, eight digits there; the last six are what a six-digit authenticator shows
    for at, expected in ((59, "94287082"), (1111111109, "07081804"), (1111111111, "14050471"), (1234567890, "89005924"), (2000000000, "69279037")):
        assert totp.code_at(RFC_SECRET, totp.counter(at)) == expected[-6:]


def test_a_code_is_good_for_its_step_and_one_either_side():
    now = 1_700_000_000.0
    here = totp.counter(now)
    assert totp.matching_counter(RFC_SECRET, totp.code_at(RFC_SECRET, here), now) == here
    assert totp.matching_counter(RFC_SECRET, totp.code_at(RFC_SECRET, here - 1), now) == here - 1  # a slow typist
    assert totp.matching_counter(RFC_SECRET, totp.code_at(RFC_SECRET, here + 1), now) == here + 1  # a clock a little ahead
    assert totp.matching_counter(RFC_SECRET, totp.code_at(RFC_SECRET, here - 2), now) is None
    assert totp.matching_counter(RFC_SECRET, "000000", now) in (None, here - 1, here, here + 1)
    assert totp.matching_counter(RFC_SECRET, "12345", now) is None and totp.matching_counter(RFC_SECRET, "", now) is None
    spaced = totp.code_at(RFC_SECRET, here)
    assert totp.matching_counter(RFC_SECRET, f"{spaced[:3]} {spaced[3:]}", now) == here  # typed as the app shows it


def test_a_code_is_never_accepted_twice():
    now = 1_700_000_000.0
    here = totp.counter(now)
    code = totp.code_at(RFC_SECRET, here)
    assert totp.matching_counter(RFC_SECRET, code, now, after=here) is None  # that step was already used
    assert totp.matching_counter(RFC_SECRET, totp.code_at(RFC_SECRET, here - 1), now, after=here) is None  # nor an older one


def test_secret_and_uri():
    a, b = totp.new_secret(), totp.new_secret()
    assert a != b and len(a) == 32 and totp.code_at(a, 1).isdigit()
    u = totp.uri(a, "fitri@nusantara.example")
    assert u.startswith("otpauth://totp/VENTHORA:fitri%40nusantara.example?secret=" + a) and "digits=6" in u and "period=30" in u
