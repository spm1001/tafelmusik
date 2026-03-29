#!/bin/bash
# Restart after git pull merges changes to src/.
# Install: ln -sf ../../deploy/post-merge.sh .git/hooks/post-merge
deploy/restart-if-changed.sh ORIG_HEAD HEAD
