"""Author identities for authorship tracking and origin filtering.

These constants are used in two ways:
1. As formatting attrs on Y.Text characters (author="claude")
2. As transaction origins for observer filtering (txn.origin != CLAUDE)

Adding a new author? Add the constant here, then use it in the MCP
tools or editor client that writes on their behalf.
"""

CLAUDE = "claude"
SAMEER = "sameer"
TEST = "test"
