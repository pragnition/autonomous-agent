#!/bin/sh
# Turns this plain folder into the git repo Jensen starts from, then removes itself so no trace
# of the build step is left behind.
set -eu
rm -- "$0"
git init -q
git config user.email yardstick@example.invalid
git config user.name yardstick
git add -A
git commit -q --allow-empty -m "start"
