"""auradefi — open-source multi-tenant crypto data aggregator.

Vezgo-style tenancy, DeBank-style DeFi position depth, Plaid wire format.
Library first, service second: import this package directly; the HTTP API
is one adapter among several, not the product.

See docs/SPEC.md for the full design contract.
"""

__version__ = "0.1.1"

__all__ = ["Auradefi", "__version__"]


def __getattr__(name: str):
    # Lazy so `import auradefi` stays dependency-light (SPEC §8:
    # import, don't call — the facade is the embedding entry point).
    if name == "Auradefi":
        from auradefi.embed.facade import Auradefi

        return Auradefi
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
