"""MessageBroker — o Broker de mensagens com estratégia publish/subscribe.

Centraliza **todas as trocas de mensagens** do sistema. É a camada de roteamento
que antes vivia espalhada dentro do `MessageServer`: agora o servidor apenas
expõe a interface RMI e delega ao Broker.

Modelo *tópico-por-usuário*:

* **Tópico** = nome canônico de um usuário.
* **subscribe(sub)** — o cliente assina o próprio tópico para receber mensagens
  (equivale a ficar ONLINE). Devolve as mensagens pendentes da fila durável.
* **publish(msg)** — publica no tópico ``msg.recipient``. O Broker entrega a
  cada assinante ativo (push via callback) e/ou deixa na fila durável do MOM
  quando não há assinante ativo (destinatário offline).

O Broker compõe três colaboradores, cada um com uma responsabilidade:

* ``MessageQueueManager`` (MOM) — **durabilidade**: fila offline + histórico.
* ``PresenceRegistry`` — estado ONLINE/OFFLINE e URI de callback de cada usuário.
* ``SubscriptionRegistry`` — quem assina cada tópico.

Concorrência: o dispatch copia a URI de callback sob o lock da presença e invoca
o callback **fora** de qualquer lock (chamada de rede potencialmente lenta),
mantendo a mesma disciplina descrita na §5 da ARQUITETURA.md.
"""
from __future__ import annotations

import Pyro5.api

from common.models import Message, canonical_name
from server.mom import MessageQueueManager
from server.presence import PresenceRegistry
from server.subscriptions import SubscriptionRegistry


class MessageBroker:
    def __init__(
        self,
        mom: MessageQueueManager,
        presence: PresenceRegistry,
        subscriptions: SubscriptionRegistry,
    ) -> None:
        self._mom = mom
        self._presence = presence
        self._subs = subscriptions

    # ------------------------------------------------------------------ #
    # Assinaturas (tópico-por-usuário)
    # ------------------------------------------------------------------ #
    def subscribe(self, subscriber: str, callback_uri: str) -> list[Message]:
        """Inscreve `subscriber` no próprio tópico e o marca ONLINE.

        Cria a fila durável (se ainda não existe), registra a URI de callback e
        devolve as mensagens pendentes por **peek** (flush não-destrutivo): elas
        só saem da fila quando confirmadas por `ack_read` (at-least-once).
        """
        key = canonical_name(subscriber)
        self._mom.create_queue(key)
        self._subs.subscribe(key, key)
        self._presence.set_online(key, callback_uri)
        pending = self._mom.peek(key)
        print(f"[subscribe] {subscriber} ({key}) assinou seu tópico "
              f"— {len(pending)} pendente(s)")
        return pending

    def unsubscribe(self, subscriber: str) -> None:
        """Cancela as assinaturas de `subscriber` e o marca OFFLINE.

        A fila durável permanece: mensagens publicadas enquanto ele está fora
        continuam sendo acumuladas para entrega no próximo subscribe.
        """
        key = canonical_name(subscriber)
        self._subs.unsubscribe_all(key)
        self._presence.set_offline(key)
        print(f"[unsubscribe] {subscriber} ({key}) saiu do(s) tópico(s)")

    def remove_subscriber(self, subscriber: str) -> None:
        """Remove por completo a presença de `subscriber` (saída do sistema)."""
        key = canonical_name(subscriber)
        self._subs.unsubscribe_all(key)
        self._presence.remove(key)

    # ------------------------------------------------------------------ #
    # Publicação
    # ------------------------------------------------------------------ #
    def publish(self, message: Message) -> str:
        """Publica `message` no tópico `message.recipient`.

        Registra no histórico permanente (nasce "não lida") e roteia para os
        assinantes ativos do tópico: entrega instantânea via callback se online,
        senão fica na fila durável. Retorna "DELIVERED" ou "QUEUED".

        O `message.recipient` deve ser a identidade canônica (chave do tópico e
        da fila no MOM).
        """
        topic = message.recipient
        self._mom.log_message(message)

        subscribers = self._subs.subscribers_of(topic)
        # Sem assinante ativo: o destinatário nunca assinou nesta sessão. Ainda
        # assim garantimos a fila do tópico para entrega offline futura.
        if not subscribers:
            self._mom.enqueue(message)
            print(f"[publish] {message.sender} -> {topic}: QUEUED "
                  f"({self._mom.count(topic)} na fila)")
            return "QUEUED"

        delivered = False
        for sub in subscribers:
            if self._deliver(sub, message):
                delivered = True

        result = "DELIVERED" if delivered else "QUEUED"
        print(f"[publish] {message.sender} -> {topic}: {result}")
        return result

    def _deliver(self, subscriber: str, message: Message) -> bool:
        """Entrega `message` a um assinante: push se online, senão enfileira.

        Sempre enfileira antes do push: a mensagem só sai da fila no `ack_read`
        (at-least-once). Se o push falhar, degrada graciosamente para offline.
        Retorna True se o push instantâneo foi bem-sucedido.
        """
        # A fila é keyed pelo tópico; no modelo tópico-por-usuário o assinante é
        # o próprio tópico, então enfileiramos na fila do assinante.
        self._mom.enqueue(message)

        uri = self._presence.get_callback_uri(subscriber)
        if uri is None:
            return False
        try:
            with Pyro5.api.Proxy(uri) as cb:
                cb.receive_message(
                    message.sender, message.body, message.timestamp, message.msg_id
                )
            return True
        except Exception as exc:  # assinante inacessível: degrada p/ offline
            print(f"[publish] falha no push p/ {subscriber} ({exc!r}); mantido na fila")
            self._presence.set_offline(subscriber)
            self._subs.unsubscribe_all(subscriber)
            return False

    def publish_rejection(
        self, rejecter: str, original_sender: str, original_body: str = ""
    ) -> None:
        """Avisa `original_sender` que `rejecter` recusou sua mensagem.

        Se o remetente original estiver online, faz push imediato via
        `notify_rejection`; se estiver offline, deixa o aviso na fila durável
        (como mensagem comum) para não se perder.
        """
        target = canonical_name(original_sender)
        uri = self._presence.get_callback_uri(target)
        if uri is not None:
            try:
                with Pyro5.api.Proxy(uri) as cb:
                    cb.notify_rejection(rejecter, original_body)
                print(f"[reject] {rejecter} recusou msg de {original_sender}")
                return
            except Exception:
                self._presence.set_offline(target)
                self._subs.unsubscribe_all(target)
        # Alvo offline: deixa um aviso na fila dele.
        notice = Message(
            sender=rejecter,
            recipient=target,
            body=f"(rejeitou sua mensagem) {original_body}".strip(),
        )
        self._mom.log_message(notice)
        self._mom.enqueue(notice)
        print(f"[reject] {rejecter} recusou msg de {original_sender} (enfileirado)")
