# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Read pending comments from the anchored comment DB.

Usage:
    uv run --script read-comments.py [TARGET]

Without TARGET, shows all unresolved comments.
With TARGET, shows comments for that target only.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from pathlib import Path
from tafelmusik.anchored import CommentStore


DB_PATH = Path.home() / ".tafelmusik-comments.db"


def main():
    if not DB_PATH.exists():
        print("No comments database found.")
        return

    store = CommentStore(DB_PATH)

    target_filter = sys.argv[1] if len(sys.argv) > 1 else None

    if target_filter:
        comments = store.list_for_target(target_filter)
    else:
        # All unresolved comments across all targets
        rows = store.db.execute(
            "SELECT * FROM comments WHERE resolved = 0 ORDER BY created"
        ).fetchall()
        from tafelmusik.anchored import _row_to_comment
        comments = [_row_to_comment(r) for r in rows]

    if not comments:
        print("No pending comments.")
        store.close()
        return

    current_target = None
    for c in comments:
        if c.target != current_target:
            current_target = c.target
            print(f"\n## {current_target}\n")

        reply = f"  (replying to {c.replies_to})" if c.replies_to else ""
        print(f"**@{c.author}**{reply}:")
        if c.quote:
            # Indent the quote
            for line in c.quote.splitlines():
                print(f"> {line}")
        print(f"{c.body}")
        print(f"_id: {c.id}_")
        print()
        store.resolve(c.id)

    store.close()


if __name__ == "__main__":
    main()
