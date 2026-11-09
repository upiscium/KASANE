"""Vault Core domain errors."""


class VaultError(RuntimeError):
    """Base class for Vault Core failures."""


class InvalidKnowledgeIdentityError(VaultError, ValueError):
    """The public vault URI does not follow the v0.1 identity grammar."""


class InvalidMetadataError(VaultError, ValueError):
    """Metadata cannot be represented as canonical JSON."""


class KnowledgeNotFoundError(VaultError):
    """No knowledge record exists for the public identity."""


class StaleRevisionError(VaultError):
    """The expected revision no longer matches current state."""


class InvalidLifecycleTransitionError(VaultError):
    """The requested lifecycle transition is not allowed by v0.1 policy."""
