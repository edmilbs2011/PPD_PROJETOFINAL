"""Registro de presença: estado (online/offline) e URI de callback por cliente.

Mantido apenas em memória — presença é por sessão. Ao reiniciar o servidor,
todos os clientes são considerados OFFLINE até se registrarem novamente.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

from common.models import Status


@dataclass
class Presence:
    contact_name: str
    status: Status = Status.OFFLINE
    callback_uri: str | None = None


class PresenceRegistry:
    """Tabela thread-safe de presença dos clientes."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._table: dict[str, Presence] = {}

    def set_online(self, name: str, callback_uri: str) -> None:
        with self._lock:
            self._table[name] = Presence(name, Status.ONLINE, callback_uri)

    def set_offline(self, name: str) -> None:
        with self._lock:
            p = self._table.get(name)
            if p:
                p.status = Status.OFFLINE
                p.callback_uri = None
            else:
                self._table[name] = Presence(name, Status.OFFLINE, None)

    def remove(self, name: str) -> None:
        with self._lock:
            self._table.pop(name, None)

    def is_online(self, name: str) -> bool:
        with self._lock:
            p = self._table.get(name)
            return bool(p and p.status is Status.ONLINE)

    def get_callback_uri(self, name: str) -> str | None:
        """Copia a URI sob lock; o callback é invocado FORA do lock (ver dispatcher)."""
        with self._lock:
            p = self._table.get(name)
            return p.callback_uri if p and p.status is Status.ONLINE else None

    def status_of(self, name: str) -> Status:
        with self._lock:
            p = self._table.get(name)
            return p.status if p else Status.UNKNOWN

    def statuses_of(self, names: list[str]) -> dict[str, str]:
        with self._lock:
            return {n: self.status_of(n).value for n in names}
