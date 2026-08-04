"""Structural validation of a webhook endpoint URL (SPEC §7.3, rule #8).

Structural ONLY — there is no IP allowlist and no manual whitelisting
step anywhere in this package; Vezgo authenticates webhooks by source-IP
allowlist and Zerion requires support to whitelist each callback URL, and
neither failure mode exists here. Localhost, RFC1918 hosts, bracketed
IPv6 literals and odd ports are all accepted: the host owns its egress
policy, we own the syntax.

What this module additionally rejects is anything ``httpx`` would refuse
or silently rewrite, because :func:`auradefi.webhooks.models.endpoint_id`
hashes the exact string and the deliverer POSTs it verbatim — a URL that
httpx rewrites would be signed for one receiver and delivered to another.
"""

from __future__ import annotations

import ipaddress

from auradefi.errors import ValidationError


#: Characters RFC 3986 allows in an authority (userinfo, host, port), and
#: the subset httpx re-emits verbatim in USERINFO — it percent-escapes
#: ``;=@[]`` there. Anything else it would reject or silently escape.
_AUTHORITY_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~%!$&'()*+,;=:@[]"
)
_USERINFO_CHARS = _AUTHORITY_CHARS - frozenset(";=@[]")

#: Characters httpx re-emits verbatim in a path, query or fragment:
#: printable ASCII less the six it escapes (below 0x20 and at 0x7f it
#: raises ``InvalidURL`` instead, from anywhere in the string).
_TAIL_CHARS = frozenset(map(chr, range(0x21, 0x7F))) - frozenset("\"<>`{}")

_HEX_CHARS = frozenset("0123456789abcdefABCDEF")

#: The port httpx DELETES from a URL because the scheme implies it.
_DEFAULT_PORT = {"http": "80", "https": "443"}


def _escapes_are_valid(authority: str) -> bool:
    """True iff every ``%`` in ``authority`` starts a two-hex-digit escape."""
    return all(len(t) > 1 and set(t[:2]) <= _HEX_CHARS for t in authority.split("%")[1:])


def _authority_is_verbatim(authority: str, scheme: str) -> bool:
    """True iff ``[userinfo@]host[:port]`` is what httpx re-emits.

    Brackets mean an IPv6 literal ``ipaddress`` accepts, optionally
    followed by ``:port``; anything else is a name with at most one
    colon, canonical digits after it, and no upper case. ``[::1/x``
    fails because httpx raises ``InvalidURL`` on it — NOT an
    ``httpx.HTTPError``, so it would escape ``Deliverer.tick``; ``H.t``,
    ``h.t:080``, ``h.t:80``, ``@h.t`` and ``u;s@h.t`` fail because httpx
    lower-cases a name, strips a leading zero, DELETES the scheme's
    default port, an empty userinfo, and escapes ``;=@[]`` in one.
    """
    userinfo, at, host = authority.rpartition("@")
    if at and not (userinfo and set(userinfo) <= _USERINFO_CHARS):
        return False
    if host.startswith("["):
        literal, bracket, port = host[1:].partition("]")
        try:
            ipaddress.IPv6Address(literal)
        except ValueError:
            return False
        if not bracket:
            return False
    else:
        name, _, tail = host.partition(":")
        port = ":" + tail if ":" in host else ""
        if "[" in host or "]" in host or not name or name != name.lower():
            return False
    digits = port[1:]
    if port and (port[0] != ":" or not digits.isdigit() or digits != str(int(digits))):
        return False
    return digits != _DEFAULT_PORT[scheme]


def validate_endpoint_url(url: str) -> str:
    """Return ``url`` UNCHANGED iff it is structurally a webhook URL.

    Structural only (rule #8: no IP allowlist, no manual whitelisting):
    the scheme must be ``http`` or ``https`` and a well-formed authority
    must follow ``://``. Localhost, RFC1918 hosts, bracketed IPv6
    literals, and odd ports are all accepted — the host owns its own
    egress policy.

    Syntax is not policy, though. The WHOLE string — authority, path,
    query and fragment — reaches ``httpx.Client.post`` verbatim, so what
    httpx cannot parse (``https://[::1/x``, any character below 0x20 →
    ``InvalidURL``) or silently rewrites (``/ x`` → ``/%20x``, ``/é`` →
    ``/%C3%A9``, ``/a/../b`` → ``/b``: another receiver) is out too.

    No normalisation whatsoever: no strip, no case-folding, no trailing
    slash munging, because :func:`endpoint_id` hashes the exact string.
    Anything else raises :class:`auradefi.errors.ValidationError`.
    """
    scheme, separator, remainder = url.partition("://")
    authority = remainder.split("/")[0].split("?")[0].split("#")[0]
    tail = remainder[len(authority) :]
    path = tail.split("?")[0].split("#")[0]
    if (
        separator
        and scheme in ("http", "https")
        and authority
        and set(authority) <= _AUTHORITY_CHARS
        and set(tail) <= _TAIL_CHARS
        and not {".", ".."} & set(path.split("/"))
        and _escapes_are_valid(authority)
        and _authority_is_verbatim(authority, scheme)
    ):
        return url
    raise ValidationError(f"webhook url must be http(s) with a host: {url!r}")
