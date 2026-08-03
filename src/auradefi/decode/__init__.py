"""decode/: raw chain records → rich Transaction(parts[], fees[], acts[])
(SPEC §4.4–§4.5). Every movement is a part; fees are siblings, never
movements; the decoder is versioned (rule #7).

Docstring-only __init__: import concrete modules, e.g.
auradefi.decode.models.
"""
