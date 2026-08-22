"""In-process turn cache.

LangGraph replays the last uncompleted node after a crash-resume. Without a
guard that re-invokes the model and re-runs every tool the turn performed.

This cache is an optimisation, not the guarantee: it is per-process and dies
with the sandbox. The durable guarantee is the backend's turn_results table,
which survives sandbox loss. Keeping both means the common case avoids a network
round trip while the uncommon case is still correct.
"""

from __future__ import annotations

from collections import OrderedDict

from .protocol import TurnResult

# A meeting is capped well below this; the bound exists so a long-lived warm
# sandbox reused across meetings cannot grow without limit.
MAX_ENTRIES = 64


class TurnCache:
    def __init__(self, max_entries: int = MAX_ENTRIES) -> None:
        self._entries: OrderedDict[str, TurnResult] = OrderedDict()
        self._max = max_entries

    def get(self, turn_key: str) -> TurnResult | None:
        result = self._entries.get(turn_key)
        if result is not None:
            self._entries.move_to_end(turn_key)
        return result

    def put(self, turn_key: str, result: TurnResult) -> None:
        self._entries[turn_key] = result
        self._entries.move_to_end(turn_key)
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)
