"""Portfolio assembly — balances + prices to Holdings (SPEC §3.1:
Account → Holding[]). Transport-free by design: adapters arrive
constructed; this domain never sees an HTTP client.

Docstring-only __init__: import concrete modules, e.g.
auradefi.portfolio.holdings.
"""
