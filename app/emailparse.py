"""EML parsing built on the standard-library ``email`` package.

Normalization rules (documented in README, kept deterministic so that batch
ordering never influences the result):

* A Message-ID is stored in its *bare* form: whitespace stripped, angle
  brackets removed (``<a@b>`` -> ``a@b``).
* ``In-Reply-To`` and ``References`` may contain several ids; every
  ``<...>`` token is extracted.  If no angle-bracket token is present the
  header is split on whitespace (tolerates broken clients).
* Parent resolution: the **last** id of ``In-Reply-To`` wins; if the header
  is absent/empty, the **last** id of ``References`` is used; otherwise the
  message is a root.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser

_ANGLE_TOKEN = re.compile(r"<([^<>]*)>")


@dataclass
class ParsedMessage:
    message_id: str | None
    in_reply_to: list[str] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    parent_id: str | None = None
    self_reference: bool = False


def _extract_ids(values: list[str]) -> list[str]:
    """Pull message-ids out of raw header values, preserving order."""
    ids: list[str] = []
    for value in values:
        tokens = _ANGLE_TOKEN.findall(value)
        if not tokens:
            tokens = value.split()
        for tok in tokens:
            tok = tok.strip().strip("<>").strip()
            if tok:
                ids.append(tok)
    return ids


def _header_values(msg, name: str) -> list[str]:
    values = msg.get_all(name)
    if not values:
        return []
    return [str(v) for v in values]


def parse_eml(raw: bytes) -> ParsedMessage:
    """Parse one raw EML payload and extract threading headers."""
    msg = BytesParser(policy=policy.default).parsebytes(raw)

    message_ids = _extract_ids(_header_values(msg, "Message-ID"))
    # Any non-empty normalized token is accepted; only absent/empty headers
    # count as "missing Message-ID".
    message_id = message_ids[0] if message_ids else None

    in_reply_to = _extract_ids(_header_values(msg, "In-Reply-To"))
    references = _extract_ids(_header_values(msg, "References"))

    if in_reply_to:
        parent = in_reply_to[-1]
    elif references:
        parent = references[-1]
    else:
        parent = None

    self_ref = message_id is not None and (
        message_id in in_reply_to or message_id in references
    )

    return ParsedMessage(
        message_id=message_id,
        in_reply_to=in_reply_to,
        references=references,
        parent_id=parent,
        self_reference=self_ref,
    )
