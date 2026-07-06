"""Registro de assinaturas do Broker (publish/subscribe).

Mantém, para cada **tópico**, o conjunto de **assinantes** (subscribers). No
modelo *tópico-por-usuário* adotado pelo Broker, cada cliente assina o tópico
que corresponde ao seu próprio nome canônico: publicar em `bob` entrega a quem
assina `bob` (isto é, o próprio Bob). Isso preserva a semântica unicast do chat
e, ao mesmo tempo, generaliza para tópicos de grupo (vários assinantes) no
futuro.

Assim como a presença, a assinatura é **por sessão** e vive apenas em memória:
ao reiniciar o servidor, ninguém está assinando até se registrar de novo. A
durabilidade das mensagens (fila offline) continua a cargo do MOM.

O acesso é protegido por um `RLock`, pois o daemon Pyro5 pode atender
requisições concorrentes.
"""
from __future__ import annotations

import threading


class SubscriptionRegistry:
    """Tabela thread-safe de assinaturas: ``topic -> {subscribers}``."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        #: tópico (canônico) -> conjunto de assinantes (canônicos)
        self._topics: dict[str, set[str]] = {}

    def subscribe(self, topic: str, subscriber: str) -> None:
        """Inscreve `subscriber` no `topic` (idempotente)."""
        with self._lock:
            self._topics.setdefault(topic, set()).add(subscriber)

    def unsubscribe(self, topic: str, subscriber: str) -> None:
        """Cancela a inscrição de `subscriber` em `topic`."""
        with self._lock:
            subs = self._topics.get(topic)
            if subs:
                subs.discard(subscriber)
                if not subs:
                    del self._topics[topic]

    def unsubscribe_all(self, subscriber: str) -> None:
        """Remove `subscriber` de todos os tópicos (usado ao sair do sistema)."""
        with self._lock:
            for topic in list(self._topics):
                subs = self._topics[topic]
                subs.discard(subscriber)
                if not subs:
                    del self._topics[topic]

    def subscribers_of(self, topic: str) -> set[str]:
        """Cópia do conjunto de assinantes de `topic` (vazio se não houver)."""
        with self._lock:
            return set(self._topics.get(topic, ()))

    def topics_of(self, subscriber: str) -> set[str]:
        """Cópia do conjunto de tópicos que `subscriber` assina."""
        with self._lock:
            return {t for t, subs in self._topics.items() if subscriber in subs}

    def is_subscribed(self, topic: str, subscriber: str) -> bool:
        with self._lock:
            return subscriber in self._topics.get(topic, ())
