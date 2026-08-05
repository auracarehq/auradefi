"""The webhook seam states everything its consumers read (§5 Wave C).

These tests are about the DECLARATION, not about any implementation. The
behavioural proof, a sink written from these Protocols alone, driven
through every webhook route, lives in
``tests/contract/seams/test_wave1_webhook_sink.py``. What is pinned here
is the property that made #27 and #28 possible in the first place: a
declared interface may not promise less than its callers require, and it
may not promise more than they use either.
"""

from __future__ import annotations

import typing

from auradefi.api.sinks import DeliveryRow, EndpointRow, EventRow, WebhookSink

#: Every member a webhook route reaches for on ``deps.webhooks``.
_SINK_MEMBERS = frozenset(
    {
        "register_endpoint",
        "endpoints",
        "emit",
        "deliveries",
        "dead_letter",
        "get_event",
        "create_replay",
    }
)


def _declared_members(protocol: type) -> frozenset[str]:
    return frozenset(
        name
        for name in dir(protocol)
        if not name.startswith("_") and callable(getattr(protocol, name, None))
    )


def test_the_sink_declares_exactly_the_members_the_routes_call():
    # pins: not a member fewer (that is #27: an unhandled 500 for every
    #       host-supplied sink) and not a member more (dead code every host
    #       must write to satisfy isinstance).
    assert _declared_members(WebhookSink) == _SINK_MEMBERS


def test_get_delivery_is_not_promised():
    # pins: the shipped store calls its OWN get_delivery internally, and no
    #       route ever calls it through the seam. Promising it would make it
    #       load-bearing for hosts without making it used.
    assert "get_delivery" not in _declared_members(WebhookSink)


def test_no_member_returns_a_bare_object():
    # pins: a bare `object` return is unimplementable from the declaration
    #       alone: it names no attribute, so a host cannot know what to
    #       build, and every attribute it omits is another 500.
    bare = []
    for name in sorted(_SINK_MEMBERS):
        hints = typing.get_type_hints(getattr(WebhookSink, name))
        declared = hints["return"]
        arguments = typing.get_args(declared)
        element = arguments[0] if arguments else declared
        if element is object:
            bare.append(f"{name} -> {declared!r}")
    assert bare == [], "a return type that names no attribute:\n" + "\n".join(bare)


def test_the_row_protocols_name_every_attribute_the_wires_read():
    # pins: the exact read surface of api/routes/admin.py's three wire
    #       projections. Restated here rather than imported so a silent
    #       narrowing of a Protocol goes red on this side too.
    assert set(EndpointRow.__annotations__) == {
        "id",
        "url",
        "events",
        "created_at_ms",
    }
    assert set(DeliveryRow.__annotations__) == {
        "id",
        "endpoint_id",
        "event_id",
        "status",
        "attempts",
        "created_at_ms",
        "next_attempt_at_ms",
        "delivered_at_ms",
        "last_status_code",
        "last_error",
        "replay_ordinal",
    }
    assert set(EventRow.__annotations__) == {"name"}


def test_the_seam_module_imports_no_webhooks_package():
    # pins: the seam stays bindable by a host that never installed the
    #       shipped stores. Naming webhooks.models here would state the
    #       return shapes correctly and re-couple the seam doing it, 
    #       tests/api/test_deps.py pins the same property for api/deps.py.
    #       Over the IMPORT GRAPH, not the source text: the docstring
    #       names `auradefi.webhooks` to say what it deliberately does not
    #       import, and a substring check would call that prose a defect.
    import ast
    import pathlib

    import auradefi.api.sinks as module

    assert module.__file__ is not None
    tree = ast.parse(pathlib.Path(module.__file__).read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)

    banned = ("auradefi.webhooks", "auradefi.portfolio")
    offenders = [
        name
        for name in imported
        if any(name == b or name.startswith(f"{b}.") for b in banned)
    ]
    assert not offenders, f"api/sinks.py must stay independent: {offenders}"
