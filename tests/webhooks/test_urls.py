"""Structural URL validation for webhook endpoints (SPEC §7.3, rule #8).

The accept cases prove there is no allowlist — localhost, RFC1918, IPv6
literals and odd ports all pass. The reject cases prove the stricter
half: anything httpx would refuse or silently rewrite is refused here,
because the URL is hashed into the endpoint id and POSTed verbatim.
"""

from __future__ import annotations

import pytest

from auradefi.errors import ValidationError
from auradefi.webhooks.urls import validate_endpoint_url


@pytest.mark.parametrize(
    "url",
    [
        "https://hooks.example.test/auradefi",
        "http://hooks.example.test/auradefi",
        "https://hooks.example.test:8443/a/b?c=d",
        # Rule #8, the named casualty: NO IP allowlist, NO whitelisting.
        "http://127.0.0.1:9000/hook",
        "http://10.0.0.7/hook",
        "http://localhost:8000/",
        # A bracketed IPv6 literal is a HOST, not junk: rejecting malformed
        # hosts must not turn into "no brackets allowed".
        "http://[::1]:9000/hook",
    ],
)
def test_validate_endpoint_url_accepts_and_never_rewrites(url):
    assert validate_endpoint_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "hooks.example.test/auradefi",
        "ftp://hooks.example.test/a",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "https://",
        "http://",
        " https://hooks.example.test/a",
    ],
)
def test_validate_endpoint_url_rejects_non_http_urls(url):
    with pytest.raises(ValidationError):
        validate_endpoint_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https:// /x",  # the host is a space — httpx silently rewrites to %20
        "https://a b.com/x",  # embedded space, likewise rewritten
        "https://[::1/x",  # unbalanced bracket — httpx raises InvalidURL
        "http://%zz/x",  # "%zz" is not a percent-escape
        "http://ho\x00st/x",  # NUL
        "http://ho\tst/x",  # tab
        "http://host\n.example.test/x",  # newline
    ],
)
def test_validate_endpoint_url_rejects_a_malformed_host(url):
    """A structural check that admits garbage is not a structural check.

    Rule #8 forbids an ALLOWLIST, not syntax. Every URL this function
    returns is later handed verbatim to ``httpx.Client.post``, so a host
    httpx cannot parse — ``https://[::1/x`` raises ``httpx.InvalidURL``,
    which is NOT an ``httpx.HTTPError`` and therefore escapes
    ``Deliverer.tick`` — must never reach the store, and one httpx
    silently rewrites (``https:// /x`` → ``https://%20/x``) must not
    either: the registered URL and the URL POSTed have to be one string.
    """
    with pytest.raises(ValidationError):
        validate_endpoint_url(url)
