#!/usr/bin/env bash
# Install run-queued and queue-dash on Linux or macOS.
#
#   curl -fsSL https://raw.githubusercontent.com/mstelz/run-queued/main/install.sh | bash
#       clones to ~/.local/share/run-queued (or updates it), then links the tools
#   ./install.sh               from a clone: links the tools from this folder
#   ./install.sh --uninstall   removes the links
#   -y, --yes                  answer yes to every question (with curl: ... | bash -s -- -y)
#
# The tools are symlinked into $BIN_DIR (default ~/.local/bin), so a `git pull` in the clone
# updates them. The Claude Code skill is linked into ~/.claude/skills if you want it.
set -euo pipefail

REPO="${RUN_QUEUED_REPO:-${QUEUE_DASH_REPO:-https://github.com/mstelz/run-queued.git}}"
CLONE_DIR="${RUN_QUEUED_DIR:-${QUEUE_DASH_DIR:-$HOME/.local/share/run-queued}}"
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"
SKILL="$HOME/.claude/skills/run-queued/SKILL.md"

say() { printf '%s\n' "$*"; }
tilde() { local t='~'; printf '%s' "${1/#$HOME/$t}"; }  # bash 3.2 keeps a \~ literally
die() { printf 'install: %s\n' "$*" >&2; exit 1; }

YES=0
UNINSTALL=0
for arg in "$@"; do
  case "$arg" in
    -y|--yes) YES=1 ;;
    --uninstall) UNINSTALL=1 ;;
    -h|--help) sed -n '2,11p' "${BASH_SOURCE[0]:-/dev/null}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option: $arg" ;;
  esac
done

# ask "question" default [default-with-no-terminal]: returns 0 for yes. Piped from curl, stdin
# is this script, so the answer is read from the terminal. -y answers yes to everything.
ask() {
  local answer hint="y/N"
  [ "$2" = y ] && hint="Y/n"
  [ "$YES" = 1 ] && return 0
  if ! (: </dev/tty) 2>/dev/null; then [ "${3:-$2}" = y ]; return; fi
  printf '%s [%s] ' "$1" "$hint" >/dev/tty
  read -r answer </dev/tty || answer=""
  case "${answer:-$2}" in [Yy]*) return 0 ;; *) return 1 ;; esac
}

# Replace dest with a symlink in one rename, so an agent calling run-queued mid-install never
# finds it missing. A real file already there is copied to .bak first.
link() {
  local target="$1" dest="$2"
  mkdir -p "$(dirname "$dest")"
  if [ -e "$dest" ] && [ ! -L "$dest" ]; then
    cp -p "$dest" "$dest.bak"
    say "  saved your old $(tilde "$dest") as $(tilde "$dest").bak"
  fi
  ln -sfn "$target" "$dest.tmp.$$"
  mv -f "$dest.tmp.$$" "$dest"
  say "  $(tilde "$dest") -> $(tilde "$target")"
}

unlink_if_ours() {
  if [ -L "$1" ]; then rm -f "$1"; say "  removed $(tilde "$1")"; fi
}

if [ "$UNINSTALL" = 1 ]; then
  unlink_if_ours "$BIN_DIR/run-queued"
  unlink_if_ours "$BIN_DIR/queue-dash"
  unlink_if_ours "$SKILL"
  say "Done. If the installer cloned it, the code is still in $(tilde "$CLONE_DIR")."
  exit 0
fi

OS="$(uname -s)"
case "$OS" in
  Linux)
    say "Installing for Linux."
    command -v flock >/dev/null ||
      say "note: flock is missing. Install util-linux (e.g. sudo apt install util-linux)."
    ;;
  Darwin)
    say "Installing for macOS."
    if ! command -v flock >/dev/null; then
      if command -v brew >/dev/null; then
        # Installing software needs a yes: with no terminal to ask, skip it unless -y was given.
        if ask "run-queued needs flock, which macOS doesn't include. Install it with Homebrew?" y n
        then
          brew install flock
        else
          say "note: run-queued won't work until you run: brew install flock"
        fi
      else
        say "note: run-queued needs flock. Install Homebrew (https://brew.sh), then: brew install flock"
      fi
    fi
    ;;
  MINGW*|MSYS*|CYGWIN*)
    die "native Windows isn't supported yet. Install it inside WSL instead:
  https://learn.microsoft.com/windows/wsl/install
A native Windows build may come later if people ask for it:
  https://github.com/mstelz/run-queued/issues"
    ;;
  *) die "unsupported system: $OS (Linux and macOS only)" ;;
esac
command -v python3 >/dev/null ||
  say "note: python3 is missing. run-queued works without it; queue-dash needs it."

# Run from a clone: use it. Piped from curl: there is no script file, so clone or update.
SRC=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  d="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  [ -f "$d/run-queued" ] && SRC="$d"
fi
if [ -z "$SRC" ]; then
  command -v git >/dev/null || die "git is needed to download run-queued"
  if [ -d "$CLONE_DIR/.git" ]; then
    say "Updating $(tilde "$CLONE_DIR")"
    git -C "$CLONE_DIR" pull --ff-only --quiet
  else
    say "Cloning into $(tilde "$CLONE_DIR")"
    git clone --quiet "$REPO" "$CLONE_DIR"
  fi
  SRC="$CLONE_DIR"
fi

say "Linking:"
link "$SRC/run-queued" "$BIN_DIR/run-queued"
link "$SRC/queue-dash" "$BIN_DIR/queue-dash"
if [ -d "$HOME/.claude" ]; then
  if [ -L "$SKILL" ] || ask "Add the Claude Code skill that tells Claude to use the queue?" y; then
    link "$SRC/skills/run-queued/SKILL.md" "$SKILL"
  fi
fi

say ""
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) say "Add $BIN_DIR to your PATH, e.g. in ~/.bashrc or ~/.zshrc:"
     say "  export PATH=\"$BIN_DIR:\$PATH\""
     say "" ;;
esac
say "Installed. Try:  run-queued -- sleep 5   and   queue-dash"
