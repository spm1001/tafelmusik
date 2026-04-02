# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Play with anchored comments on a real file.

Usage:
    uv run --script playground.py <file.md>

Commands:
    c <quote> | <body>     Create a comment (quote the text, pipe body)
    l                      List all comments with their anchor status
    r                      Re-anchor all comments against current file
    t                      Show threads
    d                      Delete the DB and start fresh
    q                      Quit

Edit the file in another window. Run 'r' to see what happens to your comments.
"""

import os
import sys
import readline  # noqa: F401 — enables line editing in input()

# Add src to path so we can import without installing
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from pathlib import Path
from tafelmusik.anchored import CommentStore, anchor, capture_context


def read_file(path: Path) -> str:
    return path.read_text()


def show_anchored(text: str, comment, result):
    """Show a comment with its context in the document."""
    if result is None:
        status = "ORPHANED"
        context = f'  (was: "{comment.quote}")'
    elif result.confident:
        status = "anchored"
        # Show the line containing the anchor
        line_start = text.rfind("\n", 0, result.start) + 1
        line_end = text.find("\n", result.end)
        if line_end == -1:
            line_end = len(text)
        line = text[line_start:line_end]
        context = f"  > {line.strip()}"
    else:
        status = "DRIFTED"
        found = text[result.start : result.end]
        context = f'  ~ "{found}"'

    resolved = " [resolved]" if comment.resolved else ""
    reply = f" (reply to {comment.replies_to[:16]}...)" if comment.replies_to else ""

    print(f"  [{status}]{resolved}{reply}")
    print(f"  @{comment.author}: {comment.body}")
    if comment.quote:
        print(context)
    if comment.prefix or comment.suffix:
        print(f'  prefix: "{comment.prefix}"')
        print(f'  suffix: "{comment.suffix}"')
    print(f"  id: {comment.id}")
    print()


def main():
    if len(sys.argv) < 2:
        print("Usage: uv run --script playground.py <file.md>")
        sys.exit(1)

    file_path = Path(sys.argv[1]).resolve()
    if not file_path.exists():
        print(f"File not found: {file_path}")
        sys.exit(1)

    try:
        target = str(file_path.relative_to(Path.home() / "Repos"))
    except ValueError:
        target = str(file_path.relative_to(Path.home()))
    db_path = file_path.parent / f".{file_path.stem}.comments.db"

    print(f"Target: {target}")
    print(f"DB: {db_path}")
    print(f"Commands: c (create) | l (list) | r (reanchor) | t (threads) | d (reset) | q (quit)")
    print()

    store = CommentStore(db_path)
    author = "sameer"

    while True:
        try:
            cmd = input(f"({author})> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not cmd:
            continue

        if cmd == "q":
            break

        elif cmd == "d":
            store.close()
            db_path.unlink(missing_ok=True)
            store = CommentStore(db_path)
            print("Fresh start.\n")

        elif cmd.startswith("c "):
            # c <quote> | <body>
            rest = cmd[2:]
            if "|" in rest:
                quote_text, body = rest.split("|", 1)
                quote_text = quote_text.strip()
                body = body.strip()
            else:
                quote_text = None
                body = rest.strip()

            text = read_file(file_path)
            prefix = suffix = None

            if quote_text:
                result = anchor(text, quote_text)
                if result is None:
                    print(f'  Quote not found: "{quote_text}"')
                    print("  Creating as unanchored comment.\n")
                    quote_text = None
                else:
                    prefix, suffix = capture_context(text, result.start, result.end)
                    if result.confident:
                        print(f"  Anchored at position {result.start}")
                    else:
                        found = text[result.start : result.end]
                        print(f'  Fuzzy match: "{found}"')

            c = store.create(
                author=author,
                target=target,
                body=body,
                quote=quote_text,
                prefix=prefix,
                suffix=suffix,
            )
            print(f"  Created: {c.id}\n")

        elif cmd.startswith("re "):
            # re <id> | <body>  — reply to a comment
            rest = cmd[3:]
            if "|" not in rest:
                print("  Usage: re <id-prefix> | <body>")
                continue
            id_prefix, body = rest.split("|", 1)
            id_prefix = id_prefix.strip()
            body = body.strip()

            # Find comment by prefix
            comments = store.list_for_target(target, include_resolved=True)
            matches = [c for c in comments if c.id.startswith(id_prefix)]
            if len(matches) != 1:
                print(f"  Found {len(matches)} comments matching '{id_prefix}'")
                continue

            parent = matches[0]
            c = store.create(
                author=author,
                target=target,
                body=body,
                replies_to=parent.id,
            )
            print(f"  Reply created: {c.id}\n")

        elif cmd == "l":
            text = read_file(file_path)
            comments = store.list_for_target(target, include_resolved=True)
            if not comments:
                print("  No comments.\n")
                continue

            for comment in comments:
                if comment.quote:
                    result = anchor(text, comment.quote, comment.prefix, comment.suffix)
                else:
                    result = None
                show_anchored(text, comment, result)

        elif cmd == "r":
            text = read_file(file_path)
            results = store.reanchor_all(target, text)
            if not results:
                print("  No comments to reanchor.\n")
                continue
            for cid, status in results.items():
                c = store.get(cid)
                short_id = cid[:20]
                print(f"  {short_id}  {status}")
            print()

        elif cmd == "t":
            comments = store.list_for_target(target, include_resolved=True)
            roots = [c for c in comments if c.replies_to is None]
            text = read_file(file_path)

            for root in roots:
                thread = store.list_thread(root.id)
                for i, c in enumerate(thread):
                    indent = "  " * (i > 0)
                    print(f"  {indent}@{c.author}: {c.body}")
                    if c.quote:
                        result = anchor(text, c.quote, c.prefix, c.suffix)
                        if result and result.confident:
                            print(f"  {indent}  > {c.quote}")
                        elif result:
                            print(f"  {indent}  ~ {text[result.start:result.end]}")
                        else:
                            print(f"  {indent}  ✗ orphaned (was: {c.quote})")
                print()

        elif cmd in ("claude", "sameer"):
            author = cmd
            print(f"  Now commenting as {author}\n")

        elif cmd.startswith("resolve "):
            id_prefix = cmd[8:].strip()
            comments = store.list_for_target(target, include_resolved=True)
            matches = [c for c in comments if c.id.startswith(id_prefix)]
            if len(matches) == 1:
                store.resolve(matches[0].id)
                print(f"  Resolved: {matches[0].id}\n")
            else:
                print(f"  Found {len(matches)} matches.\n")

        else:
            print("  Commands: c (create) | l (list) | r (reanchor) | re (reply) | t (threads) | d (reset) | q (quit)")
            print("  Switch author: claude | sameer")
            print("  Resolve: resolve <id-prefix>")
            print()

    store.close()


if __name__ == "__main__":
    main()
