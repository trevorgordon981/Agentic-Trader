"""Public API contract; production-derived narrative omitted."""

from __future__ import annotations

import sys
from typing import Sequence


REFUSAL = (
    "REFUSED: clear_stale_latches.py is retired and cannot modify exitmgr_state.json. "
    "conId/exits.log matching is not exact terminal-order evidence. Recover the exact "
    "permId/orderRef/(clientId, orderId) receipt through the protective owner, or use an "
    "explicit identity-bound adjudication workflow."
)


def main(argv: Sequence[str] | None = None) -> int:
    """Public API contract; production-derived narrative omitted."""
    del argv
    print(REFUSAL, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
