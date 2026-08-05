"""HTTP API: one adapter among several, not the product (SPEC rule #11).

A THIN shell over the importable core: every endpoint authenticates, calls
one core object, and projects. The only domain permitted to import a web
framework (tests/style/test_layering.py enforces it).
"""
