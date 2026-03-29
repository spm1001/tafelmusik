#!/bin/bash
# Unit test for the deploy hook's grep filtering logic.
# Exercises the same pipeline as restart-if-changed.sh without needing git.

PASS=0
FAIL=0

check() {
    local description="$1"
    local input="$2"
    local expect_restart="$3"  # "yes" or "no"

    result=$(echo "$input" \
        | grep '^src/tafelmusik/.*\.py$' \
        | grep -v '_test\.py$' \
        | grep -v 'conftest\.py$')

    if [ "$expect_restart" = "yes" ]; then
        if [ -n "$result" ]; then
            PASS=$((PASS + 1))
        else
            echo "FAIL: $description — expected restart, got skip"
            FAIL=$((FAIL + 1))
        fi
    else
        if [ -z "$result" ]; then
            PASS=$((PASS + 1))
        else
            echo "FAIL: $description — expected skip, got restart (matched: $result)"
            FAIL=$((FAIL + 1))
        fi
    fi
}

# Should restart
check "asgi_server.py changed"         "src/tafelmusik/asgi_server.py"       yes
check "mcp_server.py changed"          "src/tafelmusik/mcp_server.py"        yes
check "document.py changed"            "src/tafelmusik/document.py"          yes
check "new module added"               "src/tafelmusik/channel_server.py"    yes
check "mixed: src + test files"        "src/tafelmusik/asgi_server.py
src/tafelmusik/asgi_server_test.py"   yes

# Should NOT restart
check "only test file changed"         "src/tafelmusik/asgi_server_test.py"  no
check "only conftest changed"          "src/tafelmusik/conftest.py"          no
check "only CLAUDE.md changed"         "CLAUDE.md"                          no
check "only JS changed"                "public/editor.js"                    no
check "only pyproject.toml changed"    "pyproject.toml"                      no
check "deploy script changed"          "deploy/post-commit.sh"              no
check "test + conftest only"           "src/tafelmusik/document_test.py
src/tafelmusik/conftest.py"           no
check "non-py file in src"             "src/tafelmusik/README.md"            no

echo ""
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
