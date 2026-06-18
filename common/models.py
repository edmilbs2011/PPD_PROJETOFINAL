"""Modelos de dados compartilhados.

As mensagens trafegam via Pyro5 como dicionários (serialização) e são
reconstruídas como `Message` nas duas pontas. Usamos `to_dict`/`from_dict`
para deixar essa fronteira explícita.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Status(str, Enum):
    """Estado de presença de um cliente."""
    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"
    UNKNOWN = "UNKNOWN"


# ---------------------------------------------------------------------------
# Nome de contato: validação e identidade case-insensitive
# ---------------------------------------------------------------------------
#: 2 a 20 caracteres: letras (inclusive acentuadas), dígitos, '_' ou '-'.
_NAME_PATTERN = re.compile(r"[\w-]{2,20}", re.UNICODE)


def normalize_name(name: str) -> str:
    """Remove espaços nas bordas."""
    return (name or "").strip()


def validate_contact_name(name: str) -> str:
    """Critica o nome de contato; retorna o nome normalizado ou levanta ValueError."""
    n = normalize_name(name)
    if not n:
        raise ValueError("O nome de contato não pode ser vazio.")
    if not _NAME_PATTERN.fullmatch(n):
        raise ValueError(
            "Nome inválido. Use de 2 a 20 caracteres: letras, números, "
            "'_' ou '-' (sem espaços)."
        )
    return n


def canonical_name(name: str) -> str:
    """Forma canônica usada como IDENTIDADE (case-insensitive).

    'Pedro', 'pedro' e 'PEDRO' referem-se ao mesmo cliente.
    """
    return normalize_name(name).casefold()


def _now_iso() -> str:
    # Hora local COM offset de fuso (ex.: 2026-06-16T22:58:04-03:00).
    # O offset embutido permite que o destinatário exiba a hora de parede do
    # remetente inalterada, independentemente do fuso da máquina dele.
    return datetime.now().astimezone().isoformat()


@dataclass
class Message:
    """Uma mensagem trocada entre dois contatos."""
    sender: str
    recipient: str
    body: str
    timestamp: str = field(default_factory=_now_iso)
    msg_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    # -- Serialização para travessia pelo Pyro5 -----------------------------
    def to_dict(self) -> dict:
        return {
            "sender": self.sender,
            "recipient": self.recipient,
            "body": self.body,
            "timestamp": self.timestamp,
            "msg_id": self.msg_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Message":
        return cls(
            sender=data["sender"],
            recipient=data["recipient"],
            body=data["body"],
            timestamp=data.get("timestamp", _now_iso()),
            msg_id=data.get("msg_id", str(uuid.uuid4())),
        )
