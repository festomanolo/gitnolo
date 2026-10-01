#!/bin/sh
# gitnolo community installer
#   curl -fsSL https://festomanolo.com/GitNolo/install.sh | sh
# Uninstall:
#   rm -rf ~/.gitnolo/app ~/.local/bin/gitnolo
set -eu

REPO="${GITNOLO_REPO:-https://github.com/festomanolo/gitnolo}"
REF="${GITNOLO_REF:-main}"
PREFIX="${GITNOLO_PREFIX:-$HOME/.gitnolo/app}"
BIN_DIR="${GITNOLO_BIN:-$HOME/.local/bin}"

say()  { printf '  \033[38;2;196;59;85m*\033[0m %s\n' "$1"; }
fail() { printf '  \033[31mx\033[0m %s\n' "$1" >&2; exit 1; }

case "$(uname -s)" in
  Darwin|Linux) ;;
  *) fail "gitnolo supports macOS and Linux (use WSL on Windows)" ;;
esac
command -v git >/dev/null 2>&1 || fail "git is required"

PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
    PY="$c"; break
  fi
done
[ -n "$PY" ] || fail "Python 3.9 or newer is required"

say "Installing gitnolo into $PREFIX"
"$PY" -m venv "$PREFIX" || fail "could not create a virtual environment (install python3-venv)"
"$PREFIX/bin/python" -m pip install -q --upgrade pip >/dev/null 2>&1 || true
"$PREFIX/bin/python" -m pip install -q --upgrade "git+$REPO@$REF" || fail "installation failed"

mkdir -p "$BIN_DIR"
ln -sf "$PREFIX/bin/gitnolo" "$BIN_DIR/gitnolo"
say "Installed $("$PREFIX/bin/gitnolo" --version)"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) say "Add this to your shell profile:  export PATH=\"$BIN_DIR:\$PATH\"" ;;
esac
say "Next:  gitnolo doctor   then   gitnolo watch -y"
