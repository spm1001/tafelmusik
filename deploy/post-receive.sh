#!/bin/bash
# Restart when code is pushed to this repo from another machine.
# Install: ln -sf ../../deploy/post-receive.sh .git/hooks/post-receive
#
# Requires: git config receive.denyCurrentBranch updateInstead
# Remote setup (on Mac): git remote add hezza modha@hezza:Repos/batterie/tafelmusik

cd "$(git rev-parse --show-toplevel)" || exit 1

while read -r old new ref; do
    if [ "$ref" = "refs/heads/main" ]; then
        deploy/restart-if-changed.sh "$old" "$new"
    fi
done
