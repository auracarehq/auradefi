"""Recorded HTTP for the Sandbox environment — data, not code.

``sandbox.json`` is a committed recording of one address' real Etherscan V2
and DefiLlama traffic, replayed by ``auradefi.sources.sandbox`` so a
developer can run the library before holding any credential. It ships
inside the wheel; `pip install auradefi` is the only setup Sandbox needs.

A package rather than a bare directory because every directory under
``src/auradefi`` must be one (``tests/style/test_structure.py``), and
because being a regular package is what makes the fixture addressable
through ``importlib.resources`` as well as by path.

Sandbox answers are FIXED — they are a recording, not a live chain. The
numbers are constants and the docs say so wherever they appear.
"""
