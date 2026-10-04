"""End-to-end tests against a real PostgreSQL database.

Each test uses a fresh random mailbox, so the suite never needs to truncate
shared state and can run against the dev database.
"""

from __future__ import annotations

import os
import uuid

os.environ.setdefault(
    "DATABASE_URL", "postgresql://mail:mail@127.0.0.1:5432/mailmerge_test"
)

import pytest
from fastapi.testclient import TestClient

from app.main import app


def make_eml(
    mid: str,
    irt: str | None = None,
    refs: list[str] | None = None,
    subject: str = "hello",
    body: str = "body",
) -> str:
    lines = [
        "From: alice@example.com",
        "To: bob@example.com",
        f"Subject: {subject}",
        f"Message-ID: <{mid}>",
    ]
    if irt is not None:
        lines.append(f"In-Reply-To: <{irt}>")
    if refs:
        lines.append("References: " + " ".join(f"<{r}>" for r in refs))
    return "\r\n".join(lines) + "\r\n\r\n" + body + "\r\n"


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def mailbox():
    return f"t-{uuid.uuid4().hex[:16]}"


def do_import(client, mailbox, emls):
    return client.post("/imports", json={"mailbox": mailbox, "messages": emls})


def get_threads(client, mailbox, revision=None):
    params = {"mailbox": mailbox}
    if revision is not None:
        params["revision"] = revision
    return client.get("/threads", params=params)


# --------------------------------------------------------------------------
# happy path
# --------------------------------------------------------------------------

def test_single_root_import(client, mailbox):
    r = do_import(client, mailbox, [make_eml("a@x")])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["revision"] == 1
    assert body["created"] is True
    assert body["imported"] == ["a@x"]
    assert body["merges"] == []
    assert body["missing"] == []

    t = get_threads(client, mailbox).json()
    assert t["revision"] == 1 and t["head_revision"] == 1
    assert t["threads"] == [
        {
            "thread_id": "a@x",
            "root": {"message_id": "a@x", "present": True, "children": []},
        }
    ]
    assert t["missing"] == []


def test_reply_chain_and_parent_priority(client, mailbox):
    # Parent priority: In-Reply-To beats References; last References id used.
    emls = [
        make_eml("root@x"),
        make_eml("via-irt@x", irt="root@x", refs=["other@x", "ignored@x"]),
        make_eml("via-refs@x", refs=["first@x", "root@x"]),
    ]
    r = do_import(client, mailbox, emls)
    assert r.status_code == 200, r.text
    t = get_threads(client, mailbox).json()
    assert len(t["threads"]) == 1
    root = t["threads"][0]["root"]
    assert root["message_id"] == "root@x"
    kids = sorted(c["message_id"] for c in root["children"])
    assert kids == ["via-irt@x", "via-refs@x"]
    # 'first@x' is referenced but is not the chosen parent -> no placeholder
    assert t["missing"] == []


def test_placeholder_then_late_parent_merges(client, mailbox):
    # C references [A, B] -> parent is B (last reference), B missing.
    r1 = do_import(client, mailbox, [make_eml("c@x", refs=["a@x", "b@x"])])
    assert r1.status_code == 200, r1.text
    assert r1.json()["missing"] == ["b@x"]
    t1 = get_threads(client, mailbox).json()
    assert t1["threads"][0]["thread_id"] == "b@x"
    assert t1["threads"][0]["root"]["present"] is False
    assert t1["threads"][0]["root"]["children"][0]["message_id"] == "c@x"

    # Late parent B arrives, itself replying to A (still missing) -> merge.
    r2 = do_import(client, mailbox, [make_eml("b@x", irt="a@x")])
    assert r2.status_code == 200, r2.text
    assert r2.json()["merges"] == [{"old_thread_id": "b@x", "new_thread_id": "a@x"}]
    assert r2.json()["missing"] == ["a@x"]

    # Root A arrives: placeholder resolved, thread id stays A -> no merge.
    r3 = do_import(client, mailbox, [make_eml("a@x")])
    assert r3.status_code == 200, r3.text
    assert r3.json()["merges"] == []
    assert r3.json()["missing"] == []

    t = get_threads(client, mailbox).json()
    assert len(t["threads"]) == 1
    root = t["threads"][0]
    assert root["thread_id"] == "a@x"
    b = root["root"]["children"][0]
    assert b["message_id"] == "b@x" and b["present"] is True
    assert b["children"][0]["message_id"] == "c@x"


def test_merge_into_existing_thread(client, mailbox):
    do_import(client, mailbox, [make_eml("a@x")])
    do_import(client, mailbox, [make_eml("c@x", irt="b@x")])  # thread rooted at placeholder b@x
    r = do_import(client, mailbox, [make_eml("b@x", irt="a@x")])
    assert r.json()["merges"] == [{"old_thread_id": "b@x", "new_thread_id": "a@x"}]
    t = get_threads(client, mailbox).json()
    assert [th["thread_id"] for th in t["threads"]] == ["a@x"]
    a = t["threads"][0]["root"]
    assert a["children"][0]["message_id"] == "b@x"
    assert a["children"][0]["children"][0]["message_id"] == "c@x"


def test_revision_increments_and_atomicity(client, mailbox):
    assert do_import(client, mailbox, [make_eml("m1@x")]).json()["revision"] == 1
    assert do_import(client, mailbox, [make_eml("m2@x")]).json()["revision"] == 2
    assert do_import(client, mailbox, [make_eml("m3@x")]).json()["revision"] == 3
    t = get_threads(client, mailbox).json()
    assert t["head_revision"] == 3


# --------------------------------------------------------------------------
# idempotency
# --------------------------------------------------------------------------

def test_reimport_same_bytes_returns_original_revision(client, mailbox):
    eml_a, eml_b = make_eml("a@x"), make_eml("b@x", irt="a@x")
    assert do_import(client, mailbox, [eml_a]).json()["revision"] == 1
    assert do_import(client, mailbox, [eml_b]).json()["revision"] == 2

    r = do_import(client, mailbox, [eml_a])
    assert r.status_code == 200
    body = r.json()
    assert body["created"] is False
    assert body["revision"] == 1  # the original revision of a@x
    assert body["duplicates"] == ["a@x"]

    # re-importing both returns the revision where the whole batch is visible
    r2 = do_import(client, mailbox, [eml_a, eml_b])
    assert r2.json()["revision"] == 2
    assert r2.json()["created"] is False

    # head did not move
    assert get_threads(client, mailbox).json()["head_revision"] == 2


def test_duplicate_inside_batch_is_deduped(client, mailbox):
    eml = make_eml("a@x")
    r = do_import(client, mailbox, [eml, eml])
    assert r.status_code == 200
    assert r.json()["imported"] == ["a@x"]


# --------------------------------------------------------------------------
# rejections: whole batch refused, revision not advanced
# --------------------------------------------------------------------------

def test_content_conflict_with_stored_message_rejected(client, mailbox):
    do_import(client, mailbox, [make_eml("a@x", body="one")])
    r = do_import(client, mailbox, [make_eml("a@x", body="two"), make_eml("new@x")])
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "content_conflict"
    assert r.json()["detail"]["message_ids"] == ["a@x"]
    # nothing applied: new@x must not exist, revision unchanged
    t = get_threads(client, mailbox).json()
    assert t["head_revision"] == 1
    assert len(t["threads"]) == 1


def test_content_conflict_inside_batch_rejected(client, mailbox):
    r = do_import(
        client, mailbox, [make_eml("a@x", body="one"), make_eml("a@x", body="two")]
    )
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "content_conflict"


def test_missing_message_id_rejected(client, mailbox):
    eml = "From: a@b.c\r\nSubject: no id\r\n\r\nbody\r\n"
    r = do_import(client, mailbox, [eml])
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "missing_message_id"
    assert r.json()["detail"]["indices"] == [0]
    # mailbox was not even created
    assert get_threads(client, mailbox).status_code == 404


def test_self_reference_rejected(client, mailbox):
    r = do_import(client, mailbox, [make_eml("a@x", irt="a@x")])
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "self_reference"
    # self reference anywhere in References also rejected
    r2 = do_import(client, mailbox, [make_eml("b@x", refs=["b@x", "c@x"])])
    assert r2.status_code == 422
    assert r2.json()["detail"]["error"] == "self_reference"


def test_cycle_inside_batch_rejected(client, mailbox):
    r = do_import(
        client, mailbox, [make_eml("a@x", irt="b@x"), make_eml("b@x", irt="a@x")]
    )
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "cycle_detected"
    assert get_threads(client, mailbox).status_code == 404  # nothing stored


def test_cycle_via_late_parent_rejected(client, mailbox):
    assert do_import(client, mailbox, [make_eml("x@x", irt="p@x")]).status_code == 200
    # p@x arrives and points back at its own descendant -> cycle
    r = do_import(client, mailbox, [make_eml("p@x", irt="x@x")])
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "cycle_detected"
    # state untouched: p@x still a placeholder
    t = get_threads(client, mailbox).json()
    assert t["head_revision"] == 1
    assert t["missing"] == ["p@x"]


def test_empty_batch_rejected(client, mailbox):
    r = do_import(client, mailbox, [])
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "empty_batch"


# --------------------------------------------------------------------------
# determinism / isolation / history
# --------------------------------------------------------------------------

def test_batch_order_does_not_change_tree(client):
    mb1 = f"t-{uuid.uuid4().hex[:16]}"
    mb2 = f"t-{uuid.uuid4().hex[:16]}"
    emls = [make_eml("a@x"), make_eml("b@x", irt="a@x"), make_eml("c@x", irt="b@x")]
    assert do_import(client, mb1, emls).status_code == 200
    assert do_import(client, mb2, list(reversed(emls))).status_code == 200
    t1 = get_threads(client, mb1).json()
    t2 = get_threads(client, mb2).json()
    assert t1["threads"] == t2["threads"]
    assert t1["missing"] == t2["missing"]


def test_mailboxes_are_isolated(client):
    mb1 = f"t-{uuid.uuid4().hex[:16]}"
    mb2 = f"t-{uuid.uuid4().hex[:16]}"
    do_import(client, mb1, [make_eml("same@x", body="one")])
    # same Message-ID, different bytes in ANOTHER mailbox: not a conflict
    r = do_import(client, mb2, [make_eml("same@x", body="two")])
    assert r.status_code == 200
    assert get_threads(client, mb1).json()["head_revision"] == 1
    assert get_threads(client, mb2).json()["head_revision"] == 1


def test_historical_revisions_survive_merges(client, mailbox):
    do_import(client, mailbox, [make_eml("a@x")])                    # rev 1
    do_import(client, mailbox, [make_eml("c@x", irt="b@x")])         # rev 2
    do_import(client, mailbox, [make_eml("b@x", irt="a@x")])         # rev 3: merge

    t1 = get_threads(client, mailbox, revision=1).json()
    assert [th["thread_id"] for th in t1["threads"]] == ["a@x"]
    assert t1["missing"] == []

    t2 = get_threads(client, mailbox, revision=2).json()
    assert sorted(th["thread_id"] for th in t2["threads"]) == ["a@x", "b@x"]
    assert t2["missing"] == ["b@x"]

    t3 = get_threads(client, mailbox, revision=3).json()
    assert [th["thread_id"] for th in t3["threads"]] == ["a@x"]
    assert t3["missing"] == []

    # head equals the last revision
    assert get_threads(client, mailbox).json() == t3


def test_unknown_mailbox_and_revision(client, mailbox):
    assert get_threads(client, mailbox).status_code == 404
    assert get_threads(client, mailbox).json()["detail"]["error"] == "mailbox_not_found"
    do_import(client, mailbox, [make_eml("a@x")])
    r = get_threads(client, mailbox, revision=7)
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "revision_not_found"
    # revision 0 is the empty state before the first import
    t0 = get_threads(client, mailbox, revision=0).json()
    assert t0["threads"] == [] and t0["missing"] == []


def test_mixed_batch_of_new_and_duplicate(client, mailbox):
    eml_a = make_eml("a@x")
    do_import(client, mailbox, [eml_a])
    r = do_import(client, mailbox, [eml_a, make_eml("b@x", irt="a@x")])
    body = r.json()
    assert body["created"] is True
    assert body["revision"] == 2
    assert body["imported"] == ["b@x"]
    assert body["duplicates"] == ["a@x"]
