#!/usr/bin/env bash
# Reset Carino PACS to a clean slate for testing: delete ALL runtime config/data
# and local build artifacts. Source files are left untouched. Pass -y to skip
# the confirmation prompt.
set -euo pipefail
cd "$(dirname "$0")"

DATA="$HOME/CarinoPACS"
targets=(
  "$DATA"                                 # config.json + received/outgoing/sent/logs + .carinopacs_state.json
  ".venv"                                 # Python virtualenv
  "build"                                 # PyInstaller workpath (repo root)
  "desktop/node_modules"
  "desktop/dist"                          # built installers
  "desktop/engine"                        # frozen engine
  "pacs/__pycache__" "packaging/__pycache__"
  "$HOME/.config/Carino PACS"             # stray Electron userData from older builds
  "$HOME/.config/Carino-PACS"
  "$HOME/.config/carino-pacs-desktop"
)

echo "This will DELETE (source is NOT touched):"
found=0
for t in "${targets[@]}"; do [ -e "$t" ] && { echo "  - $t"; found=1; }; done
[ "$found" = 0 ] && { echo "  (nothing found — already clean)"; }

if [ "${1:-}" != "-y" ] && [ "${1:-}" != "--yes" ]; then
  read -r -p "Proceed? [y/N] " ans
  case "$ans" in y|Y) ;; *) echo "aborted"; exit 1;; esac
fi

for t in "${targets[@]}"; do rm -rf "$t"; done

cat <<'EOF'

Clean slate. Rebuild from zero:

  Headless / CLI:
    ./setup.sh                 # recreate .venv + install deps
    ./run.sh serve             # dashboard at http://127.0.0.1:8042

  Desktop app (dev):
    cd desktop && npm install && npm start

  Standalone installer:
    ./.venv/bin/python -m PyInstaller packaging/pacs-engine.spec --distpath desktop/engine --workpath build/pyi
    cd desktop && npm run dist

Tip: in the dashboard, hard-refresh (Ctrl+Shift+R) to drop cached JS/CSS.
EOF
