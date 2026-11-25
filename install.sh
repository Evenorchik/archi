#!/usr/bin/env bash
# Archi installer: one command on a fresh Ubuntu 22.04+ / Debian 12+ server, as root.
#
#   curl -fsSL https://raw.githubusercontent.com/Evenorchik/archi/main/install.sh | bash
#
# It installs the packages Archi needs, puts the code into /opt/archi (a git checkout, so `archi update`
# works), creates its Python environment, walks you through the setup (Telegram bot, Claude Code or Codex
# sign-in, optional voice key), starts the archi-telegram service and pairs your Telegram account.
# Running it again is safe: the installed code and an existing configuration are kept unless you choose
# to reconfigure.
#
# Environment (all optional):
#   ARCHI_HOME=/opt/archi       install directory
#   ARCHI_REPO=<git url>        repository to clone (default: the official one)
#   ARCHI_BRANCH=main           branch to install
#   ARCHI_SOURCE=/path          copy this local checkout instead of cloning (tests; .git is kept)
#   ARCHI_NONINTERACTIVE=1      ask nothing; the answers come from:
#     ARCHI_BOT_TOKEN, ARCHI_PROVIDER (claude|codex), ARCHI_OPENAI_KEY, ARCHI_CLAUDE_OAUTH_TOKEN,
#     ARCHI_SKIP_CLI_INSTALL=1, ARCHI_SKIP_LOGIN=1, ARCHI_SKIP_PAIRING_WAIT=1, ARCHI_RECONFIGURE=1
#
# Exit codes: 0 done · 1 unexpected error · 2 not root · 3 unsupported system · 4 no systemd ·
#             5 packages · 6 code download · 7 Python environment · 8 setup cancelled or failed · 9 service

set -Eeuo pipefail

ARCHI_HOME="${ARCHI_HOME:-/opt/archi}"
ARCHI_REPO="${ARCHI_REPO:-https://github.com/Evenorchik/archi.git}"
ARCHI_BRANCH="${ARCHI_BRANCH:-main}"
ARCHI_SOURCE="${ARCHI_SOURCE:-}"
NONINTERACTIVE="${ARCHI_NONINTERACTIVE:-0}"
PAIRING_WAIT_S="${ARCHI_PAIRING_WAIT_S:-900}"
PACKAGES=(git curl ca-certificates python3 python3-venv ffmpeg)
LOG_DIR="/var/log/archi-install"

if [ -t 1 ]; then B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; N=$'\033[0m'; else B=; G=; Y=; R=; N=; fi

say()  { printf '%s\n' "$*"; }
step() { printf '\n%s==> %s%s\n' "$B" "$*" "$N"; }
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$*" >&2; }
die()  { local code="$1"; shift; printf '\n%s✗ %s%s\n' "$R" "$*" "$N" >&2; exit "$code"; }
# A command that fails outside the checks above is a bug or a broken system: say where, exit with 1.
trap 'printf "\n%s✗ The installer stopped unexpectedly at line %s: %s%s\n" "$R" "$LINENO" "$BASH_COMMAND" "$N" >&2; exit 1' ERR

interactive() { [ "$NONINTERACTIVE" != "1" ]; }
flag() { case "${!1:-}" in 1|true|yes|on) return 0 ;; *) return 1 ;; esac; }

check_system() {
  step "Checking the system"
  [ "$(id -u)" -eq 0 ] || die 2 "Run the installer as root (for example: sudo -i, then the same command)."
  command -v apt-get >/dev/null 2>&1 || die 3 "This installer supports Debian and Ubuntu (apt) only."
  [ -r /etc/os-release ] || die 3 "Cannot identify the system (/etc/os-release is missing)."
  # shellcheck disable=SC1091
  . /etc/os-release
  case " ${ID:-} ${ID_LIKE:-} " in
    *" ubuntu "*|*" debian "*) ;;
    *) die 3 "Unsupported system: ${PRETTY_NAME:-unknown}. Archi supports Ubuntu 22.04+ and Debian 12+." ;;
  esac
  local major="${VERSION_ID%%.*}"
  if { [ "${ID:-}" = ubuntu ] && [ "${major:-0}" -lt 22 ]; } || { [ "${ID:-}" = debian ] && [ "${major:-0}" -lt 12 ]; }; then
    warn "${PRETTY_NAME:-this system} is older than supported (Ubuntu 22.04+, Debian 12+); continuing anyway."
  fi
  [ -d /run/systemd/system ] || die 4 "systemd is not running on this machine (it must be PID 1): Archi runs as a systemd service."
  if interactive && ! { : </dev/tty; } 2>/dev/null; then
    die 8 "There is no terminal to ask the setup questions on. Run the command in an interactive shell, or set ARCHI_NONINTERACTIVE=1 with ARCHI_BOT_TOKEN and the other ARCHI_* answers."
  fi
  export HOME
  HOME="$(getent passwd root | cut -d: -f6)"; HOME="${HOME:-/root}"
  export PATH="$HOME/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
  ok "${PRETTY_NAME:-Linux} with systemd, running as root"
}

install_packages() {
  step "Installing system packages"
  local missing=() p
  for p in "${PACKAGES[@]}"; do
    dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "install ok installed" || missing+=("$p")
  done
  if [ "${#missing[@]}" -eq 0 ]; then
    ok "already installed: ${PACKAGES[*]}"
    return
  fi
  mkdir -p "$LOG_DIR"
  say "  ${missing[*]} (this can take a few minutes)"
  export DEBIAN_FRONTEND=noninteractive
  if ! { apt-get update -q && apt-get install -y -q --no-install-recommends "${missing[@]}"; } >"$LOG_DIR/apt.log" 2>&1; then
    tail -n 15 "$LOG_DIR/apt.log" >&2 || true
    die 5 "Package installation failed; the full log is in $LOG_DIR/apt.log"
  fi
  python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || die 3 "Python 3.10 or newer is required (found $(python3 --version 2>&1))."
  ok "installed: ${missing[*]}"
}

copy_source() {
  [ -d "$ARCHI_SOURCE/.git" ] || die 6 "ARCHI_SOURCE=$ARCHI_SOURCE is not a git checkout."
  mkdir -p "$ARCHI_HOME"
  cp -a "$ARCHI_SOURCE/.git" "$ARCHI_HOME/.git"
  # The working tree as it is in the source (tracked and untracked files), without ignored runtime data.
  git -c safe.directory="$ARCHI_SOURCE" -C "$ARCHI_SOURCE" ls-files -z --cached --others --exclude-standard \
    | (cd "$ARCHI_SOURCE" && tar --null --no-recursion --ignore-failed-read -T - -cf -) \
    | tar -C "$ARCHI_HOME" -xf -
  git -C "$ARCHI_HOME" remote get-url origin >/dev/null 2>&1 || git -C "$ARCHI_HOME" remote add origin "$ARCHI_REPO"
}

install_code() {
  step "Installing Archi into $ARCHI_HOME"
  if [ -d "$ARCHI_HOME/.git" ]; then
    ok "existing installation found: keeping its code (update it with: archi update)"
    return
  fi
  if [ -e "$ARCHI_HOME" ] && [ -n "$(ls -A "$ARCHI_HOME" 2>/dev/null)" ]; then
    die 6 "$ARCHI_HOME exists and is not an Archi checkout; move it away or choose another ARCHI_HOME."
  fi
  if [ -n "$ARCHI_SOURCE" ]; then
    copy_source || die 6 "Copying $ARCHI_SOURCE failed."
    ok "copied from $ARCHI_SOURCE"
  else
    git clone --quiet --branch "$ARCHI_BRANCH" "$ARCHI_REPO" "$ARCHI_HOME" \
      || die 6 "Could not clone $ARCHI_REPO (branch $ARCHI_BRANCH)."
    ok "cloned $ARCHI_REPO ($ARCHI_BRANCH)"
  fi
}

setup_venv() {
  step "Preparing the Python environment"
  local venv="$ARCHI_HOME/.venv"
  if ! "$venv/bin/python" -c 'import sys' >/dev/null 2>&1; then
    rm -rf "$venv"
    python3 -m venv "$venv" >"/tmp/archi-venv.log" 2>&1 || { cat /tmp/archi-venv.log >&2; die 7 "Creating the virtual environment failed."; }
  fi
  mkdir -p "$LOG_DIR"
  if ! "$venv/bin/python" -m pip install --quiet --disable-pip-version-check -r "$ARCHI_HOME/requirements.txt" >"$LOG_DIR/pip.log" 2>&1; then
    tail -n 15 "$LOG_DIR/pip.log" >&2 || true
    die 7 "Installing the Python requirements failed; the full log is in $LOG_DIR/pip.log"
  fi
  ok "$("$venv/bin/python" --version) with the requirements in $venv"
}

link_files() {
  chmod +x "$ARCHI_HOME/archi"
  ln -sfn "$ARCHI_HOME/archi" /usr/local/bin/archi
  # Both links are part of the repository; restore them if a copy lost them.
  [ -e "$ARCHI_HOME/AGENTS.md" ] || ln -sfn CLAUDE.md "$ARCHI_HOME/AGENTS.md"
  mkdir -p "$ARCHI_HOME/.claude"
  [ -e "$ARCHI_HOME/.claude/skills" ] || ln -sfn ../skills "$ARCHI_HOME/.claude/skills"
  mkdir -p "$ARCHI_HOME/config" "$ARCHI_HOME/state" "$ARCHI_HOME/logs"
  chmod 700 "$ARCHI_HOME/config"
  ok "the archi command is ready: /usr/local/bin/archi"
}

run_setup() {
  step "Setting up your agent"
  local rc=0
  export ARCHI_HOME
  if interactive; then
    archi setup </dev/tty || rc=$?
  else
    archi setup --noninteractive || rc=$?
  fi
  [ "$rc" -eq 0 ] || die 8 "The setup did not finish. Run \`archi setup\` to complete it, then \`archi service install\`."
}

start_service() {
  step "Starting the archi-telegram service"
  archi service install || die 9 "The service did not start; see \`archi logs\` (and \`archi validate\`)."
  ok "archi-telegram is running and starts at boot"
}

pairing() {
  step "Pairing your Telegram account"
  local paired
  paired="$(ARCHI_HOME="$ARCHI_HOME" archi config get OWNER_ID)"
  if [ -n "$paired" ]; then
    ok "already paired (Telegram user $paired)"
    return
  fi
  if flag ARCHI_SKIP_PAIRING_WAIT || ! interactive; then
    archi pair
    return
  fi
  archi pair --wait "$PAIRING_WAIT_S" || true
}

summary() {
  step "Done"
  cat <<EOF
  Archi lives in $ARCHI_HOME and runs as the archi-telegram service.

  Useful commands:
    archi status                 services, provider, model, pairing
    archi logs -n 100            recent log lines
    archi restart --when-idle    restart once running tasks are done
    archi update                 update Archi and keep your local changes
    archi setup voice            add or replace the OpenAI key for voice notes
    archi pair                   show the pairing code again (or --reset to re-pair)
    archi validate               check code and config before a restart

  Talk to your agent in Telegram: give it a name and tell it how to work with you.
EOF
}

main() {
  say "${B}Archi installer${N}"
  check_system
  install_packages
  install_code
  setup_venv
  link_files
  run_setup
  start_service
  pairing
  summary
}

main "$@"
