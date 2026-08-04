"""BIP32 CKDpub contract tests (SPEC §3.2, §10; DECISIONS "BIP32 CKDpub").

Every golden here was derived by an INDEPENDENT scratch BIP32
implementation (secp256k1 affine arithmetic + base58check + bech32
written from the specs, importing no auradefi code) and only trusted
after it reproduced three PUBLISHED BIP32 test-vector facts:

    serialize(ckd_pub(parse(V1_M0H), 1))    == V1_M0H_1   (BIP32 vector 1)
    serialize(ckd_pub(parse(V2_MASTER), 0)) == V2_M0       (BIP32 vector 2)
    hash160(parse(V1_MASTER).pubkey)[:4]    == 3442193e    (BIP32 vector 1)

The derived p2wpkh/p2pkh strings were then cross-checked against the
already-green ``sources.bitcoin.encoding`` codecs — two independent
paths to the same address.

The module under test is PURE: the import allowlist is asserted here,
because an ``import httpx`` in this file would mean an extended key
could reach the wire, and SPEC §10 forbids that absolutely.
"""

from __future__ import annotations

import ast
import functools
import importlib
import inspect
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from auradefi.errors import ValidationError
from auradefi.sources.bitcoin.xpub import (
    CURVE_N,
    CURVE_P,
    GENERATOR,
    HARDENED,
    XPUB_VERSION,
    Xpub,
    ckd_pub,
    derive_addresses,
    derive_path,
    parse_xpub,
    serialize_xpub,
)

# --- published BIP32 test vectors -----------------------------------------
V1_MASTER = (
    "xpub661MyMwAqRbcFtXgS5sYJABqqG9YLmC4Q1Rdap9gSE8NqtwybGhePY2gZ29ESFjqJoC"
    "u1Rupje8YtGqsefD265TMg7usUDFdp6W1EGMcet8"
)
V1_M0H = (
    "xpub68Gmy5EdvgibQVfPdqkBBCHxA5htiqg55crXYuXoQRKfDBFA1WEjWgP6LHhwBZeNK1V"
    "TsfTFUHCdrfp1bgwQ9xv5ski8PX9rL2dZXvgGDnw"
)
V2_MASTER = (
    "xpub661MyMwAqRbcFW31YEwpkMuc5THy2PSt5bDMsktWQcFF8syAmRUapSCGu8ED9W6oDMS"
    "gv6Zz8idoc4a6mr8BDzTJY47LJhkJ8UB7WEGuduB"
)
# depth 2, child_number 4294967295 — a HARDENED ANCESTOR, which parses fine.
V_HARDENED_ANCESTOR = (
    "xpub6ASAVgeehLbnwdqV6UKMHVzgqAG8Gr6riv3Fxxpj8ksbH9ebxaEyBLZ85ySDhKiLDBr"
    "QSARLq1uNRts8RuJiHjaDMBU4Zn9h8LZNnBC5y4a"
)
ALL_VALID = (V1_MASTER, V1_M0H, V2_MASTER, V_HARDENED_ANCESTOR)

# serialize(ckd_pub(parse(V1_M0H), 1)) — BIP32 vector 1, chain m/0H/1.
V1_M0H_1 = (
    "xpub6ASuArnXKPbfEwhqN6e3mwBcDTgzisQN1wXN9BJcM47sSikHjJf3UFHKkNAWbWMiGj7"
    "Wf5uMash7SyYq527Hqck2AxYysAA7xmALppuCkwQ"
)
# serialize(ckd_pub(parse(V2_MASTER), 0)) — BIP32 vector 2, chain m/0.
V2_M0 = (
    "xpub69H7F5d8KSRgmmdJg2KhpAK8SR3DjMwAdkxj3ZuxV27CprR9LgpeyGmXUbC6wb7ERfv"
    "rnKZjXoUmmDznezpbZb7ap6r1D3tgFxHmwMkQTPH"
)

# --- goldens derived with the validated scratch implementation ------------
V1_CHAIN_CODE = bytes.fromhex(
    "873dff81c02f525623fd1fe5167eac3a55a049de3d314bb42ee227ffed37d508"
)
V1_PUBKEY = bytes.fromhex(
    "0339a36013301597daef41fbe593a02cc513d0b55527ec2df1050e2e8ff49c85c2"
)
V1_FINGERPRINT = bytes.fromhex("3442193e")
V1_M0 = (
    "xpub68Gmy5EVb2BdFbj2LpWrk1M7obNuaPTpT5oh9QCCo5sRfqSHVYWex97WpDZzszdzHzx"
    "XDAzPLVSwybe4uPYkSk4G3gnrPqqkV9RyNzAcNJ1"
)
V1_M0_CHAIN_CODE = bytes.fromhex(
    "d323f1be5af39a2d2f08f5e8f664633849653dbe329802e9847cfc85f8d7b52a"
)
V1_M0_PUBKEY = bytes.fromhex(
    "027c4b09ffb985c298afe7e5813266cbfcb7780b480ac294b0b43dc21f2be3d13c"
)
V1_M1 = (
    "xpub68Gmy5EVb2BdHTYHpekwGdcbBWax19w9HwA2DaADYvuCSSgt4YAErxxSN1KWSnmyqkw"
    "RNbnTj3XiUBKmHeC8rTjLRPjSULcDKQQgfgJDppq"
)
V1_M0_0 = (
    "xpub6AvUGrnEpfvJ8L7GLRkBTByQ9uBvUHp9o5VxHrFxhvzV4dSWkySpNaBoLR9FpbnwRmT"
    "a69yLHF3QfcaxbWT7gWdwws5k4dpmJvqpEuMWwnj"
)
V1_M0H_CHAIN_CODE = bytes.fromhex(
    "47fdacbd0f1097043b78c63c20c34ef4ed9a111d980047ad16282c7ae6236141"
)
V1_M0H_PUBKEY = bytes.fromhex(
    "035a784662a4a20a65bf6aab9ae98a6c068a81c52e4b032c0fb5400c706cfccc56"
)

# V1_MASTER m/0/i p2wpkh, i = 0..22 — the external addresses the phase-6
# gate cassette records, pinned here so a derivation change is red twice.
EXTERNAL_P2WPKH = (
    "bc1qp5wfcq48h6d63wyy9qz0awtpfqwwv4sma86mhz",
    "bc1qrfxr69jqnhwufxgkqgcdep9prq4j4vuw2wyg0v",
    "bc1qhvd6suvqzjcu9pxjhrwhtrlj85ny3n2mqql5w4",
    "bc1qjzgwzugce3mqfvn2cdq8wt8drz50mf6je9utcl",
    "bc1q4lrflkd0sddm4ktujw8e5syxmlwcdprdtzvamp",
    "bc1qc6xkeyekth5xe7lsey3qgxm55nypxe7dfpfawu",
    "bc1qmenjwqyj4kvfftjum5wlakxv4aqglf30tnd8fg",
    "bc1qkq5g90a6f7etapm5el6umchkdpdm77vupjvhmq",
    "bc1qhc6qe9fgw0f7dt4wnmu6zrfhupgmaqs3z28jdd",
    "bc1q9lzhdyu9339eky3wcd99u9rtj3erzd94tggshc",
    "bc1qfycrz5vwcqtgnjlnare9uysqqq45e482wcyu6a",
    "bc1qenugfyxfudhjsely6s0vvaap06eqsyhchnv0jx",
    "bc1qqy6cq55ksngyu3nfe0g4zr0ndp7r4h4cyqqcwv",
    "bc1qc8mufv629mxmwepa2fyuvjk5ru4tey3skx0m0e",
    "bc1q5c8ecnw5zpl9ryp0wjx05fxhhspatfewzjmv80",
    "bc1q6w8dchrynr6n9mdcms9mpmlapqs68p2ghjse90",
    "bc1ql0ya5p9g83xaa0x0d3s529y8lh0zzy28889n50",
    "bc1qrhyhwlsdxzcgtmd7vxxuhyzpew877wdjhfswg0",
    "bc1q7ws7jcfvr486xzvv706673envvztzcgld3pv6k",
    "bc1q3g86ua7nywpq05zsfw945s9ql54v76gfy54ec6",
    "bc1qxdx0aqs7qhaupgxhv06mug3cqp09z54xypq6rm",
    "bc1qn0e5umjgkqmsassm7hms84u26gj5ymwu6mh6yj",
    "bc1q8aemxc2y3nkj74ghmq69s8pelfu4rg2gsag8f0",
)
# V1_MASTER m/1/i p2wpkh, i = 0..20 (the gate's change chain).
CHANGE_P2WPKH = (
    "bc1q7zwtzcqsm3k43ha0ac7nl8cz0hqrhckywf6sew",
    "bc1qf7x2v0de6hvgv6tke54pyzmkc9022wh5j3f6y5",
    "bc1qa3ht4xx9evh8dp8p66tzftu45zccugw4lnc6nn",
    "bc1q4cunrvqcccqtn39tm8lr7ezvuurvq92nlem720",
    "bc1qaer3qp5y40x8xztq6qegnewxkmn3v6a9kaenzm",
    "bc1q6cqdvcx40upr5nzkm9kvn60dlu7l2jml6fa5k0",
    "bc1q0dq4szzsdu4fjylj7ljf3nnrddzxu9gr7vx2jv",
    "bc1qnp89rs4esd8clrrwx79rw46gw9u3eml7rhfus0",
    "bc1q9u96jy2gdhg5pewls66sj2k9hc78a25g4g2n2m",
    "bc1qqut75fra5w4k28gpr0n02fcn8xrscnwykmreae",
    "bc1qvyx2fp2g7nkjwve66erwsd8d989jsp7g7h2ppw",
    "bc1qalupy4zjf08jn3klxyvc537cmdxxqxx724aw9w",
    "bc1qra8mhpxr6emrwweuqxuvdgy2acg5sxk9t3dv4e",
    "bc1q9vqu694nwpz2rptd3qne09zzuh93pxf6m3wmj2",
    "bc1qhffvvtaqf4c6m339lnclpzqd9m4j5cwhxvxdqn",
    "bc1qx2n40nl5vskljyhslvc2kpdhdlj794kpvnu7u0",
    "bc1qj8zzuhnxz7tyjc3unva27kady9nk6uyva6yyw5",
    "bc1qh69fw88gg9kgkts4k7rv9l6wu9zlk7ppgkspcu",
    "bc1qt37fgdulwq2syqzlkve2sj60vk56lh3q9xeh87",
    "bc1qm03fadzf98n6lfvcv79ddqe0j6z7gypmzxvme7",
    "bc1qhfdgaftn5zkm44s5dwxmlyp9sjr884n8vwz8l0",
)
EXTERNAL_P2PKH = (
    "12CL4K2eVqj7hQTix7dM7CVHCkpP17Pry3",
    "13Q3u97PKtyERBpXg31MLoJbQsECgJiMMw",
    "1J4LVanjHMu3JkXbVrahNuQCTGCRRgfWWx",
)
CHANGE_0_P2PKH = "1NwEtFZ6Td7cpKaJtYoeryS6avP2TUkSMh"

# --- rejection literals, each built from V1_MASTER's own 78-byte payload ---
# version 0x0488ADE4 (an xprv with a VALID checksum — only the version is wrong)
XPRV = (
    "xprv9s21ZrQH143K3QTDL4LXw2F7HEK3wJUD2nW2nRk4stbPy6cq3jPPqjiChkVvvNKmPGJ"
    "xWUtg6LnF5kejMRNNU3TGtRBeJgk33yuGBxrMPHi"
)
# V1_MASTER's payload re-versioned 0x049D7CB2 (ypub) / 0x04B24746 (zpub)
YPUB = (
    "ypub6QqdH2c5z7967BioGSfAWFHM1EHzHPBZK7wrND3ZpEWFtzmCqvsD1bgpaE6pSAPkiSK"
    "hkuWPCJV6mZTSNMd2tK8xYTcJ48585pZecmSUzWp"
)
ZPUB = (
    "zpub6jftahH18ngZxUuv6oSniLNrBCSSE1B4EEU59bwTCEt8x6aS6b2mdfLxbS4QS53g85S"
    "WWP6wexqeer516433gYpZQoJie2tcMYdJ1SYYYAL"
)
# V1_MASTER's payload truncated to 77 bytes, re-checksummed (checksum VALID)
SHORT_PAYLOAD = (
    "Deb7pNXSbX7qSvc2eMjkNYTrggh4pBgYa2QMFjEjj6hUy1i6QK7Zm1qdZkHEwqHpT7WeE6V"
    "55dTU8PuuzPAiP8JDwAcsuN3v858r83c7mPeYLX"
)
# 78 bytes, xpub version, key 0x02 || 5 — x=5 has no square root mod p
OFF_CURVE = (
    "xpub661MyMwAqRbcFtXgS5sYJABqqG9YLmC4Q1Rdap9gSE8NqtwybGhePY2gYym6yCVZtiQ"
    "KSpLUqpuy2xafsZZR8vydJmD1kZ1yXu2LotCeeYJ"
)
# 78 bytes, xpub version, key 33 zero bytes (lead 0x00 — the xprv key marker)
ZERO_LEAD_KEY = (
    "xpub661MyMwAqRbcFtXgS5sYJABqqG9YLmC4Q1Rdap9gSE8NqtwybGhePY2gYusccjbWBs3"
    "sBBB52ffy8KP83LKyqXHsjV4oGFeBYb1Zp72k5Ee"
)
MUTATED_CHECKSUM = V1_MASTER[:-1] + "9"  # trailing '8' -> '9'


def _master() -> Xpub:
    return parse_xpub(V1_MASTER)


class TestConstants:
    """The DECISIONS-pinned secp256k1 / BIP32 numbers, asserted literally."""

    def test_curve_p(self):
        assert CURVE_P == 2**256 - 2**32 - 977
        assert CURVE_P % 4 == 3  # the (p+1)//4 square-root shortcut is valid

    def test_curve_n(self):
        # The group order stated in a base independent of the hex literal.
        assert CURVE_N == 2**256 - 432420386565659656852420866394968145599
        assert CURVE_N < CURVE_P  # n < p for secp256k1

    def test_version_and_hardened_boundary(self):
        assert XPUB_VERSION == 0x0488B21E
        assert HARDENED == 2147483648

    def test_generator_is_the_pinned_point_and_lies_on_the_curve(self):
        x, y = GENERATOR
        assert x == 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
        assert y == 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8
        assert (y * y - x * x * x - 7) % CURVE_P == 0


class TestParseXpub:
    """Decoding: field-exact goldens and every documented rejection."""

    def test_v1_master_fields(self):
        parsed = _master()
        assert parsed.depth == 0
        assert parsed.parent_fingerprint == b"\x00\x00\x00\x00"
        assert parsed.child_number == 0
        assert parsed.chain_code == V1_CHAIN_CODE
        assert parsed.pubkey == V1_PUBKEY

    def test_v1_m0h_fields_record_a_hardened_derivation(self):
        parsed = parse_xpub(V1_M0H)
        assert parsed.depth == 1
        assert parsed.parent_fingerprint == V1_FINGERPRINT
        assert parsed.child_number == 2147483648
        assert parsed.chain_code == V1_M0H_CHAIN_CODE
        assert parsed.pubkey == V1_M0H_PUBKEY

    def test_hardened_ancestor_parses_child_number_is_max_uint32(self):
        parsed = parse_xpub(V_HARDENED_ANCESTOR)
        assert parsed.child_number == 4294967295
        assert parsed.child_number >= HARDENED
        assert parsed.depth == 2

    @pytest.mark.parametrize("encoded", ALL_VALID)
    def test_pubkey_is_33_bytes_compressed(self, encoded):
        parsed = parse_xpub(encoded)
        assert len(parsed.pubkey) == 33
        assert parsed.pubkey[0] in (0x02, 0x03)
        assert len(parsed.chain_code) == 32
        assert len(parsed.parent_fingerprint) == 4

    def test_xprv_is_rejected_on_version_despite_a_valid_checksum(self):
        with pytest.raises(ValidationError):
            parse_xpub(XPRV)

    @pytest.mark.parametrize("encoded", [YPUB, ZPUB])
    def test_ypub_and_zpub_are_rejected(self, encoded):
        with pytest.raises(ValidationError):
            parse_xpub(encoded)

    def test_mutated_final_character_fails_the_checksum(self):
        assert MUTATED_CHECKSUM != V1_MASTER
        with pytest.raises(ValidationError):
            parse_xpub(MUTATED_CHECKSUM)

    def test_77_byte_payload_is_rejected(self):
        with pytest.raises(ValidationError):
            parse_xpub(SHORT_PAYLOAD)

    def test_off_curve_key_is_rejected(self):
        with pytest.raises(ValidationError):
            parse_xpub(OFF_CURVE)

    def test_zero_lead_key_byte_is_rejected(self):
        with pytest.raises(ValidationError):
            parse_xpub(ZERO_LEAD_KEY)

    @pytest.mark.parametrize("encoded", ["", "0OIl", "not-base58!"])
    def test_junk_input_is_rejected(self, encoded):
        with pytest.raises(ValidationError):
            parse_xpub(encoded)


class TestSerializeXpub:
    """Serialization is the exact inverse of parsing, byte for byte."""

    @pytest.mark.parametrize("encoded", ALL_VALID)
    def test_round_trip_identity(self, encoded):
        assert serialize_xpub(parse_xpub(encoded)) == encoded

    def test_serialize_of_a_hand_built_node_matches_the_string(self):
        built = Xpub(
            depth=0,
            parent_fingerprint=b"\x00\x00\x00\x00",
            child_number=0,
            chain_code=V1_CHAIN_CODE,
            pubkey=V1_PUBKEY,
        )
        assert serialize_xpub(built) == V1_MASTER

    def test_round_trip_survives_a_hardened_child_number(self):
        node = parse_xpub(V_HARDENED_ANCESTOR)
        assert parse_xpub(serialize_xpub(node)) == node


class TestXpubRecord:
    """The frozen record's own validation and immutability."""

    def test_is_frozen(self):
        with pytest.raises(FrozenInstanceError):
            _master().depth = 5

    def test_equality_is_by_value(self):
        assert _master() == parse_xpub(V1_MASTER)
        assert _master() != parse_xpub(V2_MASTER)

    @pytest.mark.parametrize(
        "field,value",
        [
            ("depth", -1),
            ("depth", 256),
            ("depth", True),
            ("parent_fingerprint", b"\x00\x00\x00"),
            ("child_number", -1),
            ("child_number", 2**32),
            ("child_number", True),
            ("chain_code", b"\x00" * 31),
            ("pubkey", b"\x02" * 32),
            ("pubkey", b"\x04" + b"\x01" * 32),
        ],
    )
    def test_malformed_fields_raise(self, field, value):
        fields = {
            "depth": 0,
            "parent_fingerprint": b"\x00\x00\x00\x00",
            "child_number": 0,
            "chain_code": V1_CHAIN_CODE,
            "pubkey": V1_PUBKEY,
        }
        fields[field] = value
        with pytest.raises(ValidationError):
            Xpub(**fields)


class TestCkdPub:
    """CKDpub against published vectors, plus the DECISIONS deviations."""

    def test_published_vector_1_m0h_child_1(self):
        assert serialize_xpub(ckd_pub(parse_xpub(V1_M0H), 1)) == V1_M0H_1

    def test_published_vector_2_master_child_0(self):
        assert serialize_xpub(ckd_pub(parse_xpub(V2_MASTER), 0)) == V2_M0

    def test_v1_master_child_0_fields(self):
        child = ckd_pub(_master(), 0)
        assert child.depth == 1
        assert child.parent_fingerprint == V1_FINGERPRINT
        assert child.child_number == 0
        assert child.chain_code == V1_M0_CHAIN_CODE
        assert child.pubkey == V1_M0_PUBKEY
        assert serialize_xpub(child) == V1_M0

    def test_v1_master_child_1(self):
        assert serialize_xpub(ckd_pub(_master(), 1)) == V1_M1

    def test_fingerprint_is_parent_hash160_prefix(self):
        from auradefi.sources.bitcoin.encoding import hash160

        parent = _master()
        assert ckd_pub(parent, 7).parent_fingerprint == hash160(parent.pubkey)[:4]

    def test_depth_increments_by_one_per_level(self):
        node = _master()
        for expected in (1, 2, 3):
            node = ckd_pub(node, 0)
            assert node.depth == expected

    def test_sibling_indices_give_different_keys(self):
        parent = _master()
        assert ckd_pub(parent, 0).pubkey != ckd_pub(parent, 1).pubkey

    def test_hardened_index_raises(self):
        with pytest.raises(ValidationError):
            ckd_pub(_master(), HARDENED)

    @pytest.mark.parametrize(
        "index", [2**31, 2**31 + 1, 2**32 - 1, 2**32, -1, True, 1.0, "0"]
    )
    def test_invalid_indices_raise(self, index):
        with pytest.raises(ValidationError):
            ckd_pub(_master(), index)

    def test_largest_non_hardened_index_is_derivable(self):
        child = ckd_pub(_master(), HARDENED - 1)
        assert child.child_number == 2147483647
        assert len(child.pubkey) == 33


class TestDerivePath:
    """Path walking: identity, equivalence to ckd_pub, hardened rejection."""

    @pytest.mark.parametrize("path", ["m", "M"])
    def test_bare_m_is_the_identity(self, path):
        assert derive_path(_master(), path) == _master()

    def test_single_segment_equals_ckd_pub(self):
        assert derive_path(_master(), "m/0") == ckd_pub(_master(), 0)

    def test_two_segments_golden(self):
        assert serialize_xpub(derive_path(_master(), "m/0/0")) == V1_M0_0

    def test_uppercase_m_prefix_is_accepted(self):
        assert derive_path(_master(), "M/0/0") == derive_path(_master(), "m/0/0")

    def test_depth_matches_segment_count(self):
        assert derive_path(_master(), "m/0/1/2").depth == 3

    def test_large_index_segment(self):
        assert derive_path(_master(), "m/2147483647") == ckd_pub(_master(), 2147483647)

    @pytest.mark.parametrize(
        "path",
        [
            "m/0h",
            "m/0H",
            "m/0'",
            "m/0'/1",
            "m/-1",
            "x/0",
            "0",
            "",
            "m/",
            "m//0",
            "m/1.5",
            "m/ 0",
            "m/0x1",
            "m/+1",
            "m/2147483648",
            "m/one",
        ],
    )
    def test_bad_paths_raise(self, path):
        with pytest.raises(ValidationError):
            derive_path(_master(), path)


class TestDeriveAddresses:
    """The scanner-facing API: goldens, ordering, and parameter order."""

    def test_external_p2wpkh_golden_batch(self):
        assert derive_addresses(V1_MASTER, "p2wpkh", 0, 0, 23) == EXTERNAL_P2WPKH

    def test_change_p2wpkh_golden_batch(self):
        assert derive_addresses(V1_MASTER, "p2wpkh", 1, 0, 21) == CHANGE_P2WPKH

    def test_external_p2pkh_golden(self):
        assert derive_addresses(V1_MASTER, "p2pkh", 0, 0, 3) == EXTERNAL_P2PKH

    def test_change_p2pkh_golden(self):
        assert derive_addresses(V1_MASTER, "p2pkh", 1, 0, 1) == (CHANGE_0_P2PKH,)

    def test_start_offset_is_a_window_into_the_same_chain(self):
        assert derive_addresses(V1_MASTER, "p2wpkh", 0, 5, 3) == EXTERNAL_P2WPKH[5:8]

    def test_count_zero_is_an_empty_tuple(self):
        assert derive_addresses(V1_MASTER, "p2wpkh", 0, 0, 0) == ()

    def test_returns_a_tuple_of_exactly_count_addresses(self):
        result = derive_addresses(V1_MASTER, "p2wpkh", 0, 0, 4)
        assert isinstance(result, tuple)
        assert len(result) == 4
        assert all(isinstance(address, str) for address in result)

    def test_chains_are_different_address_sets(self):
        external = set(derive_addresses(V1_MASTER, "p2wpkh", 0, 0, 21))
        change = set(derive_addresses(V1_MASTER, "p2wpkh", 1, 0, 21))
        assert not external & change

    @pytest.mark.parametrize("kind", ["p2sh", "P2WPKH", "p2tr", "", "bech32"])
    def test_bad_kind_raises(self, kind):
        with pytest.raises(ValidationError):
            derive_addresses(V1_MASTER, kind, 0, 0, 1)

    @pytest.mark.parametrize("chain", [2, -1, 3, True, "0"])
    def test_bad_chain_raises(self, chain):
        with pytest.raises(ValidationError):
            derive_addresses(V1_MASTER, "p2wpkh", chain, 0, 1)

    @pytest.mark.parametrize("start", [-1, True, "0", 1.0])
    def test_bad_start_raises(self, start):
        with pytest.raises(ValidationError):
            derive_addresses(V1_MASTER, "p2wpkh", 0, start, 1)

    @pytest.mark.parametrize("count", [-1, True, "1", 1.0])
    def test_bad_count_raises(self, count):
        with pytest.raises(ValidationError):
            derive_addresses(V1_MASTER, "p2wpkh", 0, 0, count)

    def test_a_bad_xpub_propagates_validation_error(self):
        with pytest.raises(ValidationError):
            derive_addresses(XPRV, "p2wpkh", 0, 0, 1)

    def test_signature_parameter_order_is_the_scan_contract(self):
        names = list(inspect.signature(derive_addresses).parameters)
        assert names == ["xpub", "kind", "chain", "start", "count"]

    def test_partial_yields_the_scan_derive_callable(self):
        derive = functools.partial(derive_addresses, V1_MASTER, "p2wpkh")
        assert derive(0, 0, 3) == EXTERNAL_P2WPKH[:3]
        assert derive(1, 0, 2) == CHANGE_P2WPKH[:2]
        # esplora.scan calls derive(chain, start, count) positionally.
        from auradefi.sources.bitcoin import esplora

        scan_params = list(inspect.signature(esplora.scan).parameters)
        assert scan_params[1] == "derive"

    def test_chain_node_is_derived_once_per_call(self, monkeypatch):
        import auradefi.sources.bitcoin.xpub as module

        calls: list[int] = []
        real = module.ckd_pub

        def counting(node, index):
            calls.append(index)
            return real(node, index)

        monkeypatch.setattr(module, "ckd_pub", counting)
        module.derive_addresses(V1_MASTER, "p2wpkh", 1, 4, 6)
        # exactly one chain-node derivation, then one per address
        assert calls == [1, 4, 5, 6, 7, 8, 9]


class TestPurity:
    """SPEC §10: the module cannot reach the wire, mechanically asserted."""

    ALLOWED_IMPORTS = {
        "__future__",
        "hashlib",
        "hmac",
        "dataclasses",
        "typing",
        "auradefi.errors",
        "auradefi.sources.bitcoin.encoding",
    }

    def _imports(self) -> set[str]:
        source = Path(
            importlib.import_module("auradefi.sources.bitcoin.xpub").__file__
        ).read_text(encoding="utf-8")
        found: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                found.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                found.add(node.module or "")
        return found

    def test_import_allowlist(self):
        assert self._imports() <= self.ALLOWED_IMPORTS, (
            f"xpub.py must stay pure; unexpected: "
            f"{sorted(self._imports() - self.ALLOWED_IMPORTS)}"
        )

    def test_no_httpx_and_no_esplora(self):
        imports = self._imports()
        assert not any("httpx" in name for name in imports)
        assert not any("esplora" in name for name in imports)

    def test_reimport_does_no_io(self):
        name = "auradefi.sources.bitcoin.xpub"
        saved = sys.modules.pop(name, None)
        try:
            # The autouse socket guard is active: a connect at import fails.
            module = importlib.import_module(name)
        finally:
            if saved is not None:
                sys.modules[name] = saved
        assert hasattr(module, "derive_addresses")
