"""Exception taxonomy for auradefi.

Single base class so an embedding host can catch one type at the boundary.
Exception classes are defined here and nowhere else: tests/test_errors.py
enforces that mechanically, so a new error type is a deliberate, reviewed
addition to the public contract rather than a local convenience.
"""

from __future__ import annotations


class AuradefiError(Exception):
    """Base class for every error raised by auradefi."""


class ConfigError(AuradefiError):
    """Invalid or missing configuration."""


class ValidationError(AuradefiError):
    """Input failed validation before any work was attempted."""


class CaipParseError(ValidationError):
    """A CAIP-2 or CAIP-19 identifier could not be parsed."""


class UnknownChainError(AuradefiError):
    """A chain id is syntactically valid but not in the chain registry."""


class UnknownAssetError(AuradefiError):
    """An asset id or CAIP-19 is not in the asset registry."""


class AssetConflictError(AuradefiError):
    """Registration would bind an existing CAIP-19 to a different asset."""


class DecimalsMismatchError(AuradefiError):
    """Arithmetic or aggregation across quantities of unequal decimals."""


class CurrencyMismatchError(AuradefiError):
    """Arithmetic across Money values of different currencies."""


class LedgerError(AuradefiError):
    """Base class for persistence-layer failures."""


class CursorError(LedgerError):
    """A sync cursor is malformed or belongs to a different ledger."""


class TenantIsolationError(LedgerError):
    """A call attempted to cross a tenant boundary."""


class SourceError(AuradefiError):
    """A chain, explorer, or price source failed or returned malformed data."""


class DecodeError(AuradefiError):
    """Raw records for one transaction are mutually inconsistent or do not
    involve the account being decoded.

    Distinct from SourceError: the explorer's shape was fine; the content
    cannot be decoded coherently.
    """


class QuotaExceededError(AuradefiError):
    """A tenant exhausted its quota window."""


class NotFoundError(AuradefiError):
    """The entity does not exist within the caller's tenant scope."""


class ConflictError(AuradefiError):
    """Create or update conflicts with existing state.

    Carries the existing entity's id so a UI can navigate to the conflict
    (SPEC §7.1, Vezgo's 409-with-existing_connection_id, kept).
    """

    def __init__(self, message: str, existing_id: str | None = None) -> None:
        super().__init__(message)
        self.existing_id = existing_id


class AuthError(AuradefiError):
    """Credential or token failed authentication.

    Deliberately one class for bad secret, bad signature, malformed token,
    and revoked key. Probing callers must not be able to distinguish
    failure modes (SPEC §7.2).
    """


class TokenExpiredError(AuthError):
    """A JWT's exp (ms epoch) is in the past."""


class TokenRevokedError(AuthError):
    """A JWT's jti is in the revocation set (SPEC §7.2)."""


class ScopeError(AuthError):
    """The credential lacks the scope this operation requires."""


class CassetteError(AuradefiError):
    """A cassette file is missing, malformed, or unreadable."""


class CassetteMissError(CassetteError):
    """An HTTP request had no matching interaction in the loaded cassette.

    Raised instead of letting a live call escape. The offline guarantee
    (SPEC §13) fails loudly, never silently.
    """
