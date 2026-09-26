#!/usr/bin/env bash
# One-shot installer for the Jetson Orin Nano (JetPack 6 / Ubuntu 22.04).
# Safe to re-run: it skips anything that's already done.
#
#   ./install.sh
#
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"
RUN_USER="${SUDO_USER:-$USER}"

bold() { printf '\n\033[1m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
warn() { printf '  \033[33m! %s\033[0m\n' "$*"; }

if [[ $EUID -eq 0 ]]; then
  echo "Run this as your normal user (it will ask for sudo when needed), not with sudo."
  exit 1
fi

# ---------------------------------------------------------------- platform
bold "1/6 Checking platform"
if [[ -f /etc/nv_tegra_release ]]; then
  info "Jetson detected: $(head -n1 /etc/nv_tegra_release | cut -c1-60)"
else
  warn "This doesn't look like a Jetson. Continuing anyway (it works on any Linux box with Ollama)."
fi
MEM_GB=$(awk '/MemTotal/ {printf "%d", $2/1024/1024 + 0.5}' /proc/meminfo)
info "RAM: ~${MEM_GB} GB"

# ---------------------------------------------------------------- packages
bold "2/6 Installing system packages"
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-pip curl >/dev/null
info "python: $(python3 --version)"

# ---------------------------------------------------------------- ollama
bold "3/6 Setting up Ollama"
if ! command -v ollama >/dev/null 2>&1; then
  info "Installing Ollama (official installer, detects JetPack and installs GPU support)..."
  curl -fsSL https://ollama.com/install.sh | sh
else
  info "Ollama already installed: $(ollama --version 2>/dev/null | head -n1)"
fi

# Memory-friendly settings for 8GB of shared CPU/GPU RAM.
OVERRIDE=/etc/systemd/system/ollama.service.d/override.conf
if [[ ! -f $OVERRIDE ]]; then
  info "Tuning Ollama for low memory (flash attention, 8-bit KV cache, one model at a time)"
  sudo mkdir -p "$(dirname $OVERRIDE)"
  sudo tee $OVERRIDE >/dev/null <<'EOF'
[Service]
Environment="OLLAMA_FLASH_ATTENTION=1"
Environment="OLLAMA_KV_CACHE_TYPE=q8_0"
Environment="OLLAMA_MAX_LOADED_MODELS=1"
Environment="OLLAMA_NUM_PARALLEL=1"
Environment="OLLAMA_KEEP_ALIVE=-1"
EOF
  sudo systemctl daemon-reload
fi
sudo systemctl enable --now ollama >/dev/null 2>&1 || true
sudo systemctl restart ollama
for _ in $(seq 1 30); do curl -fs http://127.0.0.1:11434/api/tags >/dev/null && break; sleep 1; done
curl -fs http://127.0.0.1:11434/api/tags >/dev/null || { echo "Ollama didn't start. Check: journalctl -u ollama"; exit 1; }
info "Ollama is running"

# ---------------------------------------------------------------- config
bold "4/6 Configuring"
set_env() {  # set_env KEY VALUE  (handles any characters in VALUE)
  python3 - "$1" "$2" <<'PY'
import re, sys
key, value = sys.argv[1], sys.argv[2]
lines = open(".env").read().splitlines()
out, found = [], False
for line in lines:
    if re.match(rf"^{re.escape(key)}=", line):
        out.append(f"{key}={value}"); found = True
    else:
        out.append(line)
if not found:
    out.append(f"{key}={value}")
open(".env", "w").write("\n".join(out) + "\n")
PY
}
get_env() { grep -E "^$1=" .env | head -n1 | cut -d= -f2-; }

if [[ ! -f .env ]]; then
  cp .env.example .env
  chmod 600 .env
  read -rp "  What should your assistant be called? [Juno] " NAME
  set_env ASSISTANT_NAME "${NAME:-Juno}"
  read -rp "  What's your first name? (optional) " UNAME
  set_env USER_NAME "$UNAME"
  while true; do
    read -rsp "  Choose a password for the web UI: " PW; echo
    [[ ${#PW} -ge 6 ]] && break
    echo "  (at least 6 characters please)"
  done
  set_env WEB_PASSWORD "$PW"
  set_env SECRET_KEY "$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
  if [[ $MEM_GB -lt 6 ]]; then
    warn "Less than 6GB RAM: using the smaller qwen3:1.7b model"
    set_env MODEL "qwen3:1.7b"
  fi
  echo
  read -rp "  Set up Discord now? You can do it later by editing .env (y/N) " DISCORD
  if [[ ${DISCORD,,} == y* ]]; then
    read -rsp "  Discord bot token: " TOKEN; echo
    set_env DISCORD_TOKEN "$TOKEN"
    read -rp "  Your Discord user ID: " OWNER
    set_env DISCORD_OWNER_ID "$OWNER"
  fi
  info "Saved settings to .env"
else
  info ".env already exists, keeping it"
fi
MODEL="$(get_env MODEL)"; MODEL="${MODEL:-qwen3:4b}"
PORT="$(get_env WEB_PORT)"; PORT="${PORT:-8080}"

# ---------------------------------------------------------------- python
bold "5/6 Installing the assistant"
[[ -d .venv ]] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
info "Python packages installed"

info "Downloading model $MODEL (a few GB, this can take a while)..."
ollama pull "$MODEL"

# ---------------------------------------------------------------- service
bold "6/6 Starting the service"
sed -e "s|__USER__|$RUN_USER|g" -e "s|__DIR__|$DIR|g" deploy/assistant.service \
  | sudo tee /etc/systemd/system/assistant.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable assistant >/dev/null 2>&1
sudo systemctl restart assistant

.venv/bin/python -m assistant check || warn "Health check reported a problem (see above)."

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
bold "All set!"
info "Open  http://${IP:-<jetson-ip>}:${PORT}  on your phone or computer (same Wi-Fi)."
info "Logs:     journalctl -u assistant -f"
info "Restart:  sudo systemctl restart assistant"
info "Terminal chat:  .venv/bin/python -m assistant chat"
