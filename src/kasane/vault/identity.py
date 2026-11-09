"""Stable public knowledge identity parsing."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import InvalidKnowledgeIdentityError

_SEGMENT = r"[A-Za-z0-9][A-Za-z0-9._~!$&'()*+,;=:@%-]{0,127}"
_OWNER_SEGMENT = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
_KNOWLEDGE_REF = re.compile(
    rf"^vault://(?:global/{_SEGMENT}(?:/{_SEGMENT})*|"
    rf"(?:project|research|private)/{_OWNER_SEGMENT}/{_SEGMENT}(?:/{_SEGMENT})*)$"
)


@dataclass(frozen=True)
class KnowledgeIdentity:
    knowledge_ref: str
    namespace: str
    knowledge_path: str


def parse_knowledge_ref(value: str) -> KnowledgeIdentity:
    """Validate and split the nested v0.1 `vault://` identity."""

    if len(value) > 2048 or not _KNOWLEDGE_REF.fullmatch(value):
        raise InvalidKnowledgeIdentityError(
            f"invalid v0.1 knowledge identity: {value!r}"
        )
    parts = value.removeprefix("vault://").split("/")
    if parts[0] == "global":
        namespace = "global"
        knowledge_path = "/".join(parts[1:])
    else:
        namespace = "/".join(parts[:2])
        knowledge_path = "/".join(parts[2:])
    return KnowledgeIdentity(value, namespace, knowledge_path)
