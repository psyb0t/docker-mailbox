#!/usr/bin/env bash
# Bump [tool.uv] exclude-newer in pyproject.toml to today's UTC midnight.
# Called by Makefile pkg-* targets before any dependency mutation so the
# supply-chain age gate is always anchored to the moment of the change.
set -euo pipefail

PYPROJECT="${1:-pyproject.toml}"
TODAY="$(date -u +%Y-%m-%dT00:00:00Z)"

if [ ! -f "$PYPROJECT" ]; then
    echo "bump_exclude_newer: $PYPROJECT not found" >&2
    exit 1
fi

if ! grep -qE '^exclude-newer\s*=' "$PYPROJECT"; then
    echo "bump_exclude_newer: no exclude-newer line in $PYPROJECT" >&2
    exit 1
fi

tmp="$(mktemp)"
sed -E "s|^(exclude-newer\s*=\s*).*|\1\"${TODAY}\"|" "$PYPROJECT" > "$tmp"
mv "$tmp" "$PYPROJECT"

echo "exclude-newer -> ${TODAY}"
