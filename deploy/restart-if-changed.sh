#!/bin/bash
# Shared deploy logic: check if non-test src/ files changed between two
# commits, restart tafelmusik.service if so, health-check afterward.
#
# Usage: restart-if-changed.sh <old-ref> <new-ref>

OLD="$1"
NEW="$2"

if [ -z "$OLD" ] || [ -z "$NEW" ]; then
    echo "Usage: restart-if-changed.sh <old-ref> <new-ref>" >&2
    exit 1
fi

CHANGED=$(git diff-tree -r --name-only --no-commit-id "$OLD" "$NEW" \
    | grep '^src/tafelmusik/.*\.py$' \
    | grep -v '_test\.py$' \
    | grep -v 'conftest\.py$')

if [ -z "$CHANGED" ]; then
    exit 0
fi

echo "tafelmusik: restarting (changed: $(echo "$CHANGED" | tr '\n' ' '))"
systemctl --user restart tafelmusik.service

for i in 1 2 3; do
    sleep 1
    if curl -sf http://127.0.0.1:3456/api/rooms > /dev/null 2>&1; then
        echo "tafelmusik: healthy"
        exit 0
    fi
done
echo "tafelmusik: WARNING — not responding after restart" >&2
