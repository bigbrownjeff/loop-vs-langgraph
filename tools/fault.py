"""Client-side fault injection, shared by both loop implementations.

LOOPLAB_DIE_AFTER_NODE=<node>[#<n>]

Hard-kills the process (os._exit, no unwinding, no atexit, no flush) at the end
of the named node's body, AFTER its tool call has completed and BEFORE the
implementation gets a chance to persist anything. `#n` picks the nth visit to
that node, 1-indexed; the default is the first visit.

This is the durability window that matters. An external SIGKILL usually lands
in the middle of a tool call, where there is nothing to lose. The expensive
case is a crash landing after a side effect has happened but before the run
state that knows about it reaches disk. os._exit is the closest thing to a
kill -9 that can be aimed precisely, and both implementations take it in
exactly the same place.
"""

from __future__ import annotations

import os

_VISITS: dict[str, int] = {}


def maybe_die(node: str) -> None:
    spec = os.environ.get("LOOPLAB_DIE_AFTER_NODE")
    if not spec:
        return
    target, _, nth = spec.partition("#")
    if target != node:
        return
    _VISITS[node] = _VISITS.get(node, 0) + 1
    if _VISITS[node] != int(nth or "1"):
        return
    print(f"DIE {node} visit {_VISITS[node]}", flush=True)
    os._exit(137)
