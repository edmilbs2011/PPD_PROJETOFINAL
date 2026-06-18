#!/usr/bin/env bash
# Sobe o Servidor de Mensagens (requer o Name Server já em execução).
set -e
cd "$(dirname "$0")"
source .venv/bin/activate 2>/dev/null || true
exec python -m server.main
