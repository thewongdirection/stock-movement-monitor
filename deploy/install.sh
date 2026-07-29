#!/usr/bin/env bash
# Install the monitor on a VPS or home box as a systemd timer.
#
#   sudo ./deploy/install.sh              # install for the invoking user
#   sudo ./deploy/install.sh --user bob   # or a named one
#
# Idempotent: safe to re-run after a git pull to pick up new units or deps.
set -euo pipefail

PREFIX=/opt/stock-movement-monitor
RUN_AS="${SUDO_USER:-$(id -un)}"
[[ "${1:-}" == "--user" ]] && RUN_AS="$2"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

[[ $EUID -eq 0 ]] || { echo "run with sudo — it writes to $PREFIX and /etc/systemd" >&2; exit 1; }
id "$RUN_AS" >/dev/null 2>&1 || { echo "no such user: $RUN_AS" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 not found" >&2; exit 1; }

python3 - <<'PY' || { echo "need Python 3.11 or newer" >&2; exit 1; }
import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)
PY

say "Installing to $PREFIX (running as $RUN_AS)"
mkdir -p "$PREFIX"
# Copy the tree, but never clobber live config or state on a re-run.
tar --exclude=.git --exclude=.venv --exclude=state --exclude=.env \
    -cf - -C "$(dirname "$0")/.." . | tar -xf - -C "$PREFIX"
mkdir -p "$PREFIX/state"

say "Building the virtualenv"
python3 -m venv "$PREFIX/.venv"
"$PREFIX/.venv/bin/pip" install --quiet --upgrade pip
"$PREFIX/.venv/bin/pip" install --quiet -r "$PREFIX/requirements.txt"

if [[ ! -f "$PREFIX/config.yaml" ]]; then
  cp "$PREFIX/config.example.yaml" "$PREFIX/config.yaml"
  echo "  wrote a starter config.yaml — put your tickers in it"
fi

if [[ ! -f "$PREFIX/.env" ]]; then
  cp "$PREFIX/deploy/env.example" "$PREFIX/.env"
  echo "  wrote a starter .env — put your credentials in it"
fi
# Credentials, so nobody else on the box reads them.
chmod 600 "$PREFIX/.env"
chown -R "$RUN_AS":"$RUN_AS" "$PREFIX"

say "Installing systemd units"
for unit in monitor.service monitor.timer monitor-bot.service; do
  # %i in the units is the user to run as; bake it in rather than using a
  # template instance, so `systemctl status monitor` reads naturally.
  sed "s/^User=%i$/User=$RUN_AS/" "$PREFIX/deploy/$unit" > "/etc/systemd/system/$unit"
done
systemctl daemon-reload

cat <<DONE

Installed. Nothing is running yet — it needs credentials first.

  1. Put your tickers in     $PREFIX/config.yaml
  2. Put your keys in        $PREFIX/.env
  3. Check it:               cd $PREFIX && sudo -u $RUN_AS .venv/bin/python -m monitor verify
  4. Start the hourly poll:  sudo systemctl enable --now monitor.timer
  5. Optional, the bot:      sudo systemctl enable --now monitor-bot

Then:
  systemctl list-timers monitor.timer     when it next fires
  journalctl -u monitor -f                what it is doing
  systemctl start monitor                 poll right now, don't wait

DONE
