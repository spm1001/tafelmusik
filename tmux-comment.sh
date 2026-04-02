#!/bin/bash
# Tmux popup for anchored comments.
# Bind in .tmux.conf:   bind C run-shell "~/Repos/batterie/tafelmusik/tmux-comment.sh"
#
# Flow: select text in tmux → prefix+C → popup → type comment → enter → stored.

SCRIPT=~/Repos/batterie/tafelmusik/comment.py

tmux display-popup -E -w 80 -h 4 -T " Comment " \
    "read -e -p '> ' body && echo \"\$body\" | uv run --script $SCRIPT"
