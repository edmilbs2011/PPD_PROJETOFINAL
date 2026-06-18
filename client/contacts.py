"""ContactBook — lista de contatos local do cliente (Requisitos 1 e 8).

A lista é propriedade do cliente e persiste em JSON. A UI a exibe o tempo todo.

Todos os nomes são guardados em MAIÚSCULAS para que a identificação de contatos
NÃO diferencie maiúsculas de minúsculas (mesma política do nome do cliente).
"""
from __future__ import annotations

import json
from pathlib import Path


def _norm(name: str) -> str:
    """Normaliza um nome de contato: sem espaços nas bordas e em MAIÚSCULAS."""
    return (name or "").strip().upper()


class ContactBook:
    def __init__(self, path: Path, owner: str = "") -> None:
        self._path = path
        #: nome do próprio cliente — não pode ser adicionado como contato.
        self._owner = _norm(owner)
        self._contacts: list[str] = []
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                raw = json.loads(self._path.read_text())
                # Normaliza/deduplica (limpa também arquivos antigos com caixa mista).
                self._contacts = sorted({_norm(n) for n in raw if _norm(n)})
            except (json.JSONDecodeError, OSError):
                self._contacts = []

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._contacts, ensure_ascii=False, indent=2))

    def all(self) -> list[str]:
        return list(self._contacts)

    def add(self, name: str) -> bool:
        """Inclui um contato. Retorna False se já existia, for inválido ou si mesmo."""
        name = _norm(name)
        if not name or name == self._owner or name in self._contacts:
            return False
        self._contacts.append(name)
        self._contacts.sort()
        self._save()
        return True

    def remove(self, name: str) -> bool:
        """Exclui um contato. Retorna False se não existia."""
        name = _norm(name)
        if name not in self._contacts:
            return False
        self._contacts.remove(name)
        self._save()
        return True

    def __contains__(self, name: str) -> bool:
        return _norm(name) in self._contacts
