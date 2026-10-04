"""Import and query logic.

Every accepted import is applied inside **one** database transaction that

1. locks the mailbox row (serializing concurrent imports per mailbox),
2. validates the whole batch against the stored state,
3. inserts the new messages + one revision row and bumps the head revision.

Any validation failure raises :class:`Reject`, the transaction rolls back and
the revision sequence does not advance.
"""

from __future__ import annotations

import hashlib

from psycopg.types.json import Jsonb

from . import emailparse, threads


class Reject(Exception):
    """Domain-level rejection -> HTTP error, transaction rolled back."""

    def __init__(self, status: int, detail: dict):
        self.status = status
        self.detail = detail
        super().__init__(str(detail))


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _missing_at(conn, mailbox_id: int, revision: int) -> list[str]:
    """Placeholder ids (referenced as parent, not imported) as of ``revision``."""
    rows = conn.execute(
        """
        SELECT DISTINCT m.parent_id
        FROM messages m
        WHERE m.mailbox_id = %s
          AND m.revision <= %s
          AND m.parent_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM messages p
              WHERE p.mailbox_id = %s
                AND p.revision <= %s
                AND p.message_id = m.parent_id
          )
        ORDER BY 1
        """,
        (mailbox_id, revision, mailbox_id, revision),
    ).fetchall()
    return [r[0] for r in rows]


def import_batch(conn, mailbox: str, raws: list[bytes]) -> dict:
    """Apply one EML batch to ``mailbox``; return the import report."""
    if not raws:
        raise Reject(422, {"error": "empty_batch", "message": "messages must not be empty"})

    # ---- parse (deterministic, order-independent) -------------------------
    parsed = [emailparse.parse_eml(raw) for raw in raws]

    no_id = sorted(i for i, p in enumerate(parsed) if not p.message_id)
    if no_id:
        raise Reject(
            422,
            {
                "error": "missing_message_id",
                "message": "every message must carry a Message-ID header",
                "indices": no_id,
            },
        )

    by_id: dict[str, emailparse.ParsedMessage] = {}
    sha_of: dict[str, str] = {}
    raw_of: dict[str, bytes] = {}
    for p, raw in zip(parsed, raws):
        digest = _sha256(raw)
        if p.message_id in by_id:
            if sha_of[p.message_id] != digest:
                raise Reject(
                    422,
                    {
                        "error": "content_conflict",
                        "message": "same Message-ID with different bytes inside the batch",
                        "message_ids": [p.message_id],
                    },
                )
            continue  # identical duplicate inside the batch: dedupe silently
        by_id[p.message_id] = p
        sha_of[p.message_id] = digest
        raw_of[p.message_id] = raw

    self_refs = sorted(mid for mid, p in by_id.items() if p.self_reference)
    if self_refs:
        raise Reject(
            422,
            {
                "error": "self_reference",
                "message": "a message must not reference its own Message-ID",
                "message_ids": self_refs,
            },
        )

    ids = sorted(by_id)

    # ---- lock the mailbox (serializes imports, gapless revisions) ---------
    conn.execute(
        "INSERT INTO mailboxes (name) VALUES (%s) ON CONFLICT (name) DO NOTHING",
        (mailbox,),
    )
    mb_id, head = conn.execute(
        "SELECT id, head_revision FROM mailboxes WHERE name = %s FOR UPDATE",
        (mailbox,),
    ).fetchone()

    # ---- dedupe against stored messages / detect content conflicts --------
    existing: dict[str, tuple[str, int]] = {}
    for mid, sha, rev in conn.execute(
        "SELECT message_id, sha256, revision FROM messages "
        "WHERE mailbox_id = %s AND message_id = ANY(%s)",
        (mb_id, ids),
    ):
        existing[mid] = (sha, rev)

    conflicts = sorted(mid for mid, (sha, _rev) in existing.items() if sha != sha_of[mid])
    if conflicts:
        raise Reject(
            422,
            {
                "error": "content_conflict",
                "message": "Message-ID already stored with different bytes",
                "message_ids": conflicts,
            },
        )

    duplicate_ids = sorted(existing)
    new_ids = [mid for mid in ids if mid not in existing]

    if not new_ids:
        # Full no-op retry: return the original revision -- the earliest
        # revision at which every message of the batch was already visible.
        revision = max(rev for _sha, rev in existing.values())
        return {
            "mailbox": mailbox,
            "revision": revision,
            "created": False,
            "imported": [],
            "duplicates": duplicate_ids,
            "merges": [],
            "missing": _missing_at(conn, mb_id, revision),
        }

    # ---- graph analysis ----------------------------------------------------
    # Which of the new ids are currently placeholders (referenced as parent)?
    resolved = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT parent_id FROM messages "
            "WHERE mailbox_id = %s AND parent_id = ANY(%s)",
            (mb_id, new_ids),
        )
    }

    # Ancestor closure of the batch's parents (existing messages only).
    seed_parents = sorted(
        {by_id[m].parent_id for m in new_ids if by_id[m].parent_id is not None}
    )
    db_parent: dict[str, str | None] = {}
    if seed_parents:
        for mid, parent in conn.execute(
            """
            WITH RECURSIVE anc AS (
                SELECT message_id, parent_id FROM messages
                WHERE mailbox_id = %s AND message_id = ANY(%s)
                UNION
                SELECT m.message_id, m.parent_id
                FROM messages m JOIN anc a ON m.message_id = a.parent_id
            )
            SELECT message_id, parent_id FROM anc
            """,
            (mb_id, seed_parents),
        ):
            db_parent[mid] = parent

    batch_parent = {m: by_id[m].parent_id for m in new_ids}
    parent_of = {**db_parent, **batch_parent}
    known = set(parent_of)

    roots: dict[str, str] = {}
    try:
        for mid in new_ids:
            threads.compute_root(mid, parent_of, known, roots)
    except threads.CycleError as err:
        raise Reject(
            422,
            {
                "error": "cycle_detected",
                "message": "applying the batch would create a reply cycle",
                "cycle": err.nodes,
            },
        ) from err

    # A merge happens when a message that used to be a placeholder (hence the
    # root of its own thread) arrives with a parent: its whole thread is
    # re-rooted under the parent's component.
    merges = [
        {"old_thread_id": mid, "new_thread_id": roots[mid]}
        for mid in sorted(resolved)
        if batch_parent[mid] is not None and roots[mid] != mid
    ]

    # ---- apply atomically ---------------------------------------------------
    revision = head + 1
    conn.execute(
        "INSERT INTO revisions (mailbox_id, revision, imported, merges) "
        "VALUES (%s, %s, %s, %s)",
        (mb_id, revision, Jsonb(new_ids), Jsonb(merges)),
    )
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO messages "
            "(mailbox_id, message_id, parent_id, in_reply_to, refs, sha256, raw, revision) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            [
                (
                    mb_id,
                    mid,
                    by_id[mid].parent_id,
                    Jsonb(by_id[mid].in_reply_to),
                    Jsonb(by_id[mid].references),
                    sha_of[mid],
                    raw_of[mid],
                    revision,
                )
                for mid in new_ids
            ],
        )
    conn.execute(
        "UPDATE mailboxes SET head_revision = %s WHERE id = %s",
        (revision, mb_id),
    )

    return {
        "mailbox": mailbox,
        "revision": revision,
        "created": True,
        "imported": new_ids,
        "duplicates": duplicate_ids,
        "merges": merges,
        "missing": _missing_at(conn, mb_id, revision),
    }


def get_threads(conn, mailbox: str, revision: int | None) -> dict:
    """Return the thread forest of ``mailbox`` as of ``revision`` (default head)."""
    row = conn.execute(
        "SELECT id, head_revision FROM mailboxes WHERE name = %s",
        (mailbox,),
    ).fetchone()
    if row is None:
        raise Reject(404, {"error": "mailbox_not_found", "mailbox": mailbox})
    mb_id, head = row

    if revision is None:
        revision = head
    if revision < 0 or revision > head:
        raise Reject(
            404,
            {
                "error": "revision_not_found",
                "mailbox": mailbox,
                "revision": revision,
                "head_revision": head,
            },
        )

    # History is immutable, so filtering by revision yields a consistent
    # snapshot even if a newer revision commits concurrently.
    rows = conn.execute(
        "SELECT message_id, parent_id FROM messages "
        "WHERE mailbox_id = %s AND revision <= %s",
        (mb_id, revision),
    ).fetchall()

    forest, missing = threads.build_forest(rows)
    return {
        "mailbox": mailbox,
        "revision": revision,
        "head_revision": head,
        "threads": forest,
        "missing": missing,
    }
