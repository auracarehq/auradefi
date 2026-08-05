"""Embedding surface (SPEC §8): import, don't call.

A host application with its own Python backend adopts auradefi as a
library, no HTTP hop, no separate service. Single-tenant in Phase 5:
the tenant id derives deterministically from the host's opaque
``external_user_id`` (SPEC §7.1 get-or-create); full tenancy wiring is
Phase 8.
"""
