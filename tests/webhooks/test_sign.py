"""The v1 webhook signature (SPEC §7.3; DECISIONS "Webhook signature (v1)").

Vezgo authenticates webhooks by SOURCE-IP ALLOWLIST — unusable behind
PaaS ingress and no evidence about the payload. This scheme replaces it,
and nothing in auradefi consults an IP allowlist or a whitelisted URL.

The golden signatures were derived INDEPENDENTLY via ``python3 -c``:

    hmac.new(secret.encode("utf-8"),
             f"{timestamp_ms}.{body}".encode("utf-8"),
             hashlib.sha256).hexdigest()

with secret ``"0123456789abcdef" * 4`` and ts ``1754000000000``. The
first one is quoted verbatim in docs/DECISIONS.md; the second signs the
golden delivery body from tests/webhooks/test_models.py.
"""

from __future__ import annotations

import pytest

from auradefi.errors import AuthError
from auradefi.webhooks.sign import (
    DEFAULT_TOLERANCE_MS,
    DELIVERY_HEADER,
    EVENT_HEADER,
    SIGNATURE_HEADER,
    SIGNATURE_VERSION,
    TIMESTAMP_HEADER,
    sign,
    verify_signature,
)

SECRET = "0123456789abcdef" * 4
TS = 1_754_000_000_000
TOLERANCE = 300_000

BODY_A1 = '{"a":1}'
GOLDEN_SIG_A1 = "v1=4e8c4d45b7e6229f4173bf35cc087c7162f427ab38a68f1578c99f0d60ccbd53"

GOLDEN_BODY = (
    '{"created_at_ms":1754000000000,'
    '"data":{"connection_id":"conn_abc123","kind":"address"},'
    '"delivery_id":"dlv_cb33eb38d1b7aa44",'
    '"event_id":"evt_490b3195618c4099",'
    '"type":"connection.created"}'
)
GOLDEN_SIG_BODY = "v1=e069ecc1386854577d0f985109556586c4c441cd90beb92af97747efef4401af"


def _flip_last_hex(signature: str) -> str:
    last = signature[-1]
    return signature[:-1] + ("0" if last != "0" else "1")


# ----------------------------------------------------------- constants


def test_header_names_are_pinned():
    assert SIGNATURE_VERSION == "v1"
    assert SIGNATURE_HEADER == "X-Auradefi-Signature"
    assert TIMESTAMP_HEADER == "X-Auradefi-Timestamp"
    assert EVENT_HEADER == "X-Auradefi-Event"
    assert DELIVERY_HEADER == "X-Auradefi-Delivery"


def test_default_tolerance_is_five_minutes_in_ms():
    assert DEFAULT_TOLERANCE_MS == 300_000 == 5 * 60 * 1000


# ------------------------------------------------------------- signing


def test_sign_golden_from_decisions():
    assert sign(SECRET, TS, BODY_A1) == GOLDEN_SIG_A1


def test_sign_golden_delivery_body():
    assert sign(SECRET, TS, GOLDEN_BODY) == GOLDEN_SIG_BODY


def test_signature_shape_is_v1_plus_sixty_four_lowercase_hex():
    signature = sign(SECRET, TS, GOLDEN_BODY)
    assert signature.startswith("v1=")
    digest = signature.removeprefix("v1=")
    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")
    assert len(signature) == 67


def test_sign_is_deterministic():
    assert sign(SECRET, TS, BODY_A1) == sign(SECRET, TS, BODY_A1)


def test_sign_binds_the_timestamp_the_body_and_the_secret():
    base = sign(SECRET, TS, BODY_A1)
    assert sign(SECRET, TS + 1, BODY_A1) != base
    assert sign(SECRET, TS, '{"a":2}') != base
    assert sign(SECRET + "0", TS, BODY_A1) != base
    # Whitespace is a different body: the signature covers exact bytes.
    assert sign(SECRET, TS, '{"a": 1}') != base


def test_sign_handles_unicode_and_empty_bodies():
    for body in ("", '{"k":"\\u00e9"}', '{"k":"é"}'):
        signature = sign(SECRET, TS, body)
        assert signature.startswith("v1=") and len(signature) == 67


# -------------------------------------------------------- verification


def test_verify_accepts_a_fresh_signature_and_returns_none():
    assert verify_signature(SECRET, TS, GOLDEN_BODY, GOLDEN_SIG_BODY, TS) is None


def test_verify_accepts_both_tolerance_boundaries_inclusively():
    signature = sign(SECRET, TS, GOLDEN_BODY)
    assert verify_signature(SECRET, TS, GOLDEN_BODY, signature, TS + TOLERANCE) is None
    assert verify_signature(SECRET, TS, GOLDEN_BODY, signature, TS - TOLERANCE) is None


def test_verify_rejects_one_millisecond_past_the_window():
    signature = sign(SECRET, TS, GOLDEN_BODY)
    for now_ms in (TS + TOLERANCE + 1, TS - TOLERANCE - 1):
        with pytest.raises(AuthError):
            verify_signature(SECRET, TS, GOLDEN_BODY, signature, now_ms)


def test_verify_honours_an_explicit_tolerance():
    signature = sign(SECRET, TS, GOLDEN_BODY)
    assert (
        verify_signature(SECRET, TS, GOLDEN_BODY, signature, TS, tolerance_ms=0) is None
    )
    with pytest.raises(AuthError):
        verify_signature(SECRET, TS, GOLDEN_BODY, signature, TS + 1, tolerance_ms=0)
    assert (
        verify_signature(
            SECRET, TS, GOLDEN_BODY, signature, TS + 86_400_000, tolerance_ms=86_400_000
        )
        is None
    )


def test_verify_rejects_a_single_flipped_hex_digit():
    with pytest.raises(AuthError):
        verify_signature(
            SECRET, TS, GOLDEN_BODY, _flip_last_hex(GOLDEN_SIG_BODY), TS
        )


def test_verify_rejects_a_single_changed_body_byte():
    mutated = GOLDEN_BODY.replace("conn_abc123", "conn_abc124")
    assert len(mutated) == len(GOLDEN_BODY)
    with pytest.raises(AuthError):
        verify_signature(SECRET, TS, mutated, GOLDEN_SIG_BODY, TS)


@pytest.mark.parametrize(
    "signature",
    [
        "4e8c4d45b7e6229f4173bf35cc087c7162f427ab38a68f1578c99f0d60ccbd53",  # no v1=
        "v2=4e8c4d45b7e6229f4173bf35cc087c7162f427ab38a68f1578c99f0d60ccbd53",
        "v1=",
        "",
        "v1=notevenhex",
        "V1=4E8C4D45B7E6229F4173BF35CC087C7162F427AB38A68F1578C99F0D60CCBD53",
        "v1=" + "0" * 64,
        "v1=v1=4e8c4d45b7e6229f4173bf35cc087c7162f427ab38a68f1578c99f0d60ccbd53",
    ],
)
def test_verify_rejects_malformed_or_unprefixed_signatures(signature):
    with pytest.raises(AuthError):
        verify_signature(SECRET, TS, BODY_A1, signature, TS)


def test_verify_rejects_the_wrong_secret():
    with pytest.raises(AuthError):
        verify_signature("f" * 64, TS, BODY_A1, GOLDEN_SIG_A1, TS)


def test_verify_rejects_a_signature_for_another_timestamp():
    with pytest.raises(AuthError):
        verify_signature(SECRET, TS + 1, BODY_A1, GOLDEN_SIG_A1, TS + 1)


def test_every_failure_is_the_same_class_with_the_same_message():
    signature = sign(SECRET, TS, GOLDEN_BODY)
    failures = [
        # stale in the future, stale in the past, bad signature, bad body,
        # missing prefix, wrong secret
        (SECRET, TS, GOLDEN_BODY, signature, TS + TOLERANCE + 1),
        (SECRET, TS, GOLDEN_BODY, signature, TS - TOLERANCE - 1),
        (SECRET, TS, GOLDEN_BODY, _flip_last_hex(signature), TS),
        (SECRET, TS, GOLDEN_BODY + " ", signature, TS),
        (SECRET, TS, GOLDEN_BODY, signature.removeprefix("v1="), TS),
        ("00" * 32, TS, GOLDEN_BODY, signature, TS),
    ]
    messages = set()
    for secret, ts, body, provided, now_ms in failures:
        with pytest.raises(AuthError) as caught:
            verify_signature(secret, ts, body, provided, now_ms)
        assert type(caught.value) is AuthError  # never a narrower subclass
        messages.add(str(caught.value))
    assert len(messages) == 1, f"failure modes are distinguishable: {sorted(messages)}"


def test_the_failure_message_leaks_neither_secret_nor_signature():
    with pytest.raises(AuthError) as caught:
        verify_signature(SECRET, TS, GOLDEN_BODY, GOLDEN_SIG_A1, TS)
    message = str(caught.value)
    assert SECRET not in message
    assert GOLDEN_SIG_A1 not in message
    assert GOLDEN_SIG_A1.removeprefix("v1=") not in message


def test_round_trip_over_huge_and_zero_timestamps():
    for ts in (0, 1, 10**15):
        signature = sign(SECRET, ts, GOLDEN_BODY)
        assert verify_signature(SECRET, ts, GOLDEN_BODY, signature, ts) is None
