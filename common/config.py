"""Configuração central: nomes lógicos no Pyro5, hosts e caminhos de dados.

Mantém em um único lugar tudo que servidor e cliente precisam combinar para se
encontrarem via Pyro5 Name Server.
"""
from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Nomes lógicos registrados no Pyro5 Name Server
# ---------------------------------------------------------------------------
#: Nome do objeto remoto do Servidor de Mensagens.
SERVER_NAME = "PPD.messageserver"

#: Prefixo dos callbacks dos clientes. Cada cliente registra "PPD.client.<nome>".
CLIENT_NAME_PREFIX = "PPD.client."


def client_logical_name(contact_name: str) -> str:
    """Nome lógico do callback de um cliente no Name Server."""
    return f"{CLIENT_NAME_PREFIX}{contact_name}"


# ---------------------------------------------------------------------------
# Rede
# ---------------------------------------------------------------------------
#: Host onde os daemons Pyro5 escutam. Use "0.0.0.0" para acesso em rede.
PYRO_HOST = os.environ.get("PPD_HOST", "localhost")

#: Host/porta do Name Server (None = autodescoberta por broadcast).
NS_HOST = os.environ.get("PPD_NS_HOST") or None
NS_PORT = int(os.environ.get("PPD_NS_PORT", "9090"))

#: Timeout (s) das chamadas remotas, para não travar a UI indefinidamente.
COMM_TIMEOUT = 5.0


# ---------------------------------------------------------------------------
# Persistência
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent

SERVER_DATA_DIR = BASE_DIR / "server_data"
CLIENT_DATA_DIR = BASE_DIR / "client_data"

#: Banco SQLite das filas offline (MOM).
MOM_DB_PATH = SERVER_DATA_DIR / "mom.db"


def contacts_path(contact_name: str) -> Path:
    """Arquivo JSON com a lista de contatos local de um cliente."""
    return CLIENT_DATA_DIR / f"{contact_name}_contacts.json"


def ensure_data_dirs() -> None:
    """Cria os diretórios de dados se ainda não existirem."""
    SERVER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    CLIENT_DATA_DIR.mkdir(parents=True, exist_ok=True)
