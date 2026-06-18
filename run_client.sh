#!/usr/bin/env bash
# Sobe um cliente. Uso: ./run_client.sh <nome_de_contato>
set -e
cd "$(dirname "$0")"
source .venv/bin/activate 2>/dev/null || true
exec python -m client.main "$@"
