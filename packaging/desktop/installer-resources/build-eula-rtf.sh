#!/usr/bin/env bash
# build-eula-rtf.sh — Regenerate EULA.rtf from the plain-text LICENSE so the
# RTF is always the renderable form of the legal source of truth.
#
# Delegates to build-eula-rtf.py which:
#   1. Splits LICENSE on blank lines and joins paragraph continuation lines
#      so the installer view renders reflowed text (not hard-wrapped 72-char
#      lines).
#   2. Appends ACKNOWLEDGMENT OF RISK block if not already in LICENSE.
#   3. Emits RTF with the ACKNOWLEDGMENT header bold at 16pt and body bold
#      at 14pt; everything else is 12pt Helvetica.
#
# Runs from build-macos.sh; safe to run manually.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"

exec python3 "$SCRIPT_DIR/build-eula-rtf.py" "$REPO_ROOT/LICENSE" "$SCRIPT_DIR/EULA.rtf"
