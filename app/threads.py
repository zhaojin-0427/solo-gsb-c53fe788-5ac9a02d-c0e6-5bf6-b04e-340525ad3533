"""Pure thread-graph algorithms (no I/O).

The thread forest is derived from immutable parent pointers:

* nodes  = imported messages + *placeholders* (ids referenced as a parent but
  not yet imported);
* edges  = child -> parent;
* a thread is one connected component; its id (``thread_id``) is the
  Message-ID of the component's root -- either a parentless message or a
  placeholder.
"""

from __future__ import annotations


class CycleError(Exception):
    """Raised when following parent pointers loops back on itself."""

    def __init__(self, nodes: list[str]):
        self.nodes = nodes
        super().__init__("cycle detected: " + " -> ".join(nodes))


def compute_root(
    start: str,
    parent_of: dict[str, str | None],
    known: set[str],
    memo: dict[str, str],
) -> str:
    """Return the root id of ``start``'s component.

    ``parent_of`` maps every known message id to its parent (``None`` for
    roots); ``known`` is the set of real messages.  A node that is not known
    is a placeholder and therefore a root.  Results are memoized in ``memo``
    (path compression).  Raises :class:`CycleError` if the walk loops.
    """
    cached = memo.get(start)
    if cached is not None:
        return cached

    path: list[str] = []
    cur = start
    while True:
        hit = memo.get(cur)
        if hit is not None:
            root = hit
            break
        if cur not in known:
            root = cur  # placeholder -> root of its component
            break
        parent = parent_of[cur]
        if parent is None:
            root = cur  # real root message
            break
        if cur in path:
            raise CycleError(path[path.index(cur):] + [cur])
        path.append(cur)
        cur = parent

    for node in path:
        memo[node] = root
    memo[start] = root
    return root


def build_forest(rows: list[tuple[str, str | None]]):
    """Build the thread forest from ``(message_id, parent_id)`` rows.

    Returns ``(threads, missing)`` where ``threads`` is a list of
    ``{"thread_id", "root"}`` entries sorted by thread id, and ``missing``
    is the sorted list of placeholder ids.  Children are sorted by
    message-id so the output is deterministic and independent of the order
    in which messages arrived within a batch.
    """
    parent_of: dict[str, str | None] = dict(rows)
    present = set(parent_of)

    children: dict[str, list[str]] = {}
    roots: list[str] = []
    for mid, parent in parent_of.items():
        if parent is None:
            roots.append(mid)
        else:
            children.setdefault(parent, []).append(mid)

    missing = sorted({p for p in children if p not in present})
    roots.extend(missing)

    def build(node: str) -> dict:
        tree = {"message_id": node, "present": node in present, "children": []}
        stack = [(node, tree)]
        while stack:
            nid, obj = stack.pop()
            for child_id in sorted(children.get(nid, [])):
                child = {
                    "message_id": child_id,
                    "present": child_id in present,
                    "children": [],
                }
                obj["children"].append(child)
                stack.append((child_id, child))
        return tree

    threads = [
        {"thread_id": root, "root": build(root)} for root in sorted(roots)
    ]
    return threads, missing
