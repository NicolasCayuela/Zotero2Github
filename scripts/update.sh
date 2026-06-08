#!/usr/bin/env bash
#
# Rebuild the Collections/ tree from a fresh Zotero export, then commit and push.
#
# Run this AFTER re-exporting your Zotero library (Format: BibLaTeX, with the
# "Export Files" option enabled) INTO THIS FOLDER, overwriting the .bib and
# regenerating the files/ folder.
#
# Place this script, update.ps1 and zotero_sync.py at the ROOT of your library
# repository (next to the .bib and files/).
#
# Usage:
#   ./update.sh                                  # default DB location
#   ZOTERO_DB="/path/to/zotero.sqlite" ./update.sh
#   NO_PUSH=1 ./update.sh                         # commit locally, do not push
#
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO"

ZOTERO_DB="${ZOTERO_DB:-$HOME/Zotero/zotero.sqlite}"

if [ ! -d "$REPO/files" ]; then
    echo "ERROR: the 'files/' folder is missing. Re-export your Zotero library" >&2
    echo "       (with 'Export Files' enabled) into this folder, then run again." >&2
    exit 1
fi
if [ ! -f "$ZOTERO_DB" ]; then
    echo "ERROR: Zotero database not found: $ZOTERO_DB" >&2
    echo "       Set ZOTERO_DB=/path/to/zotero.sqlite and retry." >&2
    exit 1
fi

PY="$(command -v python3 || command -v python || true)"
if [ -z "$PY" ]; then echo "ERROR: Python not found in PATH." >&2; exit 1; fi

# 1. Copy the database (works even while Zotero is running)
TMP="$(mktemp -t zot_sync.XXXXXX.sqlite)"
trap 'rm -f "$TMP"' EXIT
cp "$ZOTERO_DB" "$TMP"
echo "Zotero database copied."

# 2. Rebuild the collection tree
"$PY" "$REPO/zotero_sync.py" --repo "$REPO" --db "$TMP"

# 3. Commit and push
git add -A
if [ -z "$(git diff --cached --name-only)" ]; then
    echo "Nothing to commit."
    exit 0
fi
git commit -m "Update Zotero library ($(date '+%Y-%m-%d %H:%M'))" >/dev/null
echo "Committed."

if [ "${NO_PUSH:-0}" = "1" ]; then
    echo "Local commit only (NO_PUSH=1). Run 'git push' later."
else
    git push
    echo "Pushed to GitHub."
fi
