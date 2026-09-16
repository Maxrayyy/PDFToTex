"""Durable per-source/page request reservations, including interrupted requests."""

import fcntl
import json
from pathlib import Path

from ..core.textio import write_utf8_atomic


class PageReviewBudgetExceeded(ValueError):
    pass


def reserve_page_request(path, source_hash, page, limit):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Reserve before network I/O. Separate lock file survives atomic replacement.
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            state = json.loads(path.read_text())
        except FileNotFoundError:
            state = {}
        if not isinstance(state, dict) or any(type(v) is not int or v < 0 for v in state.values()):
            raise ValueError('Invalid page review budget; manual inspection required')
        key = f'{source_hash}:{page}'
        count = state.get(key, 0)
        if count >= limit:
            raise PageReviewBudgetExceeded('Full-page review request limit reached; original values need manual review')
        state[key] = count + 1
        write_utf8_atomic(path, json.dumps(state, sort_keys=True))
        return count + 1
