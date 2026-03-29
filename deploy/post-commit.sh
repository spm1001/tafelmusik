#!/bin/bash
# Restart after local commits that change src/.
# Install: ln -sf ../../deploy/post-commit.sh .git/hooks/post-commit
deploy/restart-if-changed.sh HEAD~1 HEAD
