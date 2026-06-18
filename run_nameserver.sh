#!/usr/bin/env bash
# Sobe o Pyro5 Name Server. Use --host 0.0.0.0 para acesso em rede.
set -e
cd "$(dirname "$0")"
source .venv/bin/activate 2>/dev/null || true
exec python -m Pyro5.nameserver "$@"
