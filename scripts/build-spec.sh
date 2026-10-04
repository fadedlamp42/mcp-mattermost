#!/usr/bin/env bash
# rebuild openapi.yaml from mattermost's own spec sources at a given server tag.
#
# mattermost no longer publishes a compiled spec; api/Makefile `cat`s the
# fragments under api/v4/source/ together (introduction first, definitions
# last). this mirrors that order. pin the tag to the version the server
# reports (GET /api/v4/config/client?format=old -> Version), never master:
# master documents endpoints an older server 404s on.
#
# usage: scripts/build-spec.sh v11.5.1
set -euo pipefail

tag="${1:?usage: build-spec.sh <mattermost tag, e.g. v11.5.1>}"
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT

git clone -q --depth 1 --branch "$tag" --filter=blob:none --sparse \
  https://github.com/mattermost/mattermost "$scratch/mattermost"
git -C "$scratch/mattermost" sparse-checkout set api >/dev/null

api="$scratch/mattermost/api"
grep -oE 'cat \$\(V4_SRC\)/[a-z_]+\.yaml' "$api/Makefile" \
  | sed 's/.*\///' \
  | grep -vE '^(introduction|definitions)\.yaml$' > "$scratch/order.txt"

{
  cat "$api/v4/source/introduction.yaml"
  while read -r fragment; do cat "$api/v4/source/$fragment"; done < "$scratch/order.txt"
  cat "$api/v4/source/definitions.yaml"
} > "$repo_root/openapi.yaml"

echo "$tag" > "$repo_root/openapi.version"
echo "built openapi.yaml from mattermost $tag ($(wc -l < "$scratch/order.txt") fragments)"
