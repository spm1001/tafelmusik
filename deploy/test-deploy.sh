#!/bin/bash
# Integration test for deploy mechanism.
# Verifies restart-if-changed.sh against real git history.

cd "$(git rev-parse --show-toplevel)" || exit 1

PASS=0
FAIL=0

assert_eq() {
    local desc="$1" got="$2" want="$3"
    if [ "$got" = "$want" ]; then
        PASS=$((PASS + 1))
    else
        echo "FAIL: $desc — got '$got', want '$want'"
        FAIL=$((FAIL + 1))
    fi
}

# Record service state before tests
PID_BEFORE=$(systemctl --user show tafelmusik.service -p MainPID --value 2>/dev/null)

# Test 1: commit that changed asgi_server.py should trigger restart
# Find a commit that changed asgi_server.py
SRC_COMMIT=$(git log --oneline --diff-filter=M -- src/tafelmusik/asgi_server.py | head -1 | cut -d' ' -f1)
if [ -n "$SRC_COMMIT" ]; then
    OUTPUT=$(deploy/restart-if-changed.sh "${SRC_COMMIT}~1" "$SRC_COMMIT" 2>&1)
    assert_eq "src commit triggers restart message" \
        "$(echo "$OUTPUT" | grep -c 'restarting')" "1"
    assert_eq "src commit triggers health check" \
        "$(echo "$OUTPUT" | grep -c 'healthy')" "1"
else
    echo "SKIP: no commit found that modified asgi_server.py"
fi

# Test 2: commit that only changed CLAUDE.md should NOT trigger restart
MD_COMMIT=$(git log --oneline -- CLAUDE.md | head -1 | cut -d' ' -f1)
if [ -n "$MD_COMMIT" ]; then
    # Check that this commit didn't also change src/ py files
    SRC_IN_MD=$(git diff-tree -r --name-only --no-commit-id "${MD_COMMIT}~1" "$MD_COMMIT" \
        | grep '^src/tafelmusik/.*\.py$' | grep -v '_test\.py$' | grep -v 'conftest\.py$')
    if [ -z "$SRC_IN_MD" ]; then
        OUTPUT=$(deploy/restart-if-changed.sh "${MD_COMMIT}~1" "$MD_COMMIT" 2>&1)
        assert_eq "docs-only commit produces no output" "$OUTPUT" ""
    else
        echo "SKIP: CLAUDE.md commit also changed src/ files"
    fi
else
    echo "SKIP: no commit found that modified CLAUDE.md"
fi

# Test 3: restart-if-changed.sh with no args should error
OUTPUT=$(deploy/restart-if-changed.sh 2>&1)
assert_eq "no args shows usage" \
    "$(echo "$OUTPUT" | grep -c 'Usage')" "1"

echo ""
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
