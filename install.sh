#!/bin/sh
# Antigravity Plus installer for macOS and Linux (Windows: install.cmd). Everything happens in install.py:
#   ./install.sh                     install / update into ~/.gemini/config/plugins/antigravity-plus
#   ./install.sh --uninstall         remove the plugin (memory and settings are kept)
#   ./install.sh --uninstall --purge also remove its memory and settings (~/.claude memory is never touched)
cd "$(dirname "$0")" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required (macOS: xcode-select --install; Linux: install it with your package manager)" >&2
  exit 1
fi
exec python3 install.py "$@"
