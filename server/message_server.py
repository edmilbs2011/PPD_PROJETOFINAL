"""Objeto remoto principal: MessageServer (interface §3.1 da ARQUITETURA.md).

É a **fachada RMI** exposta pelo servidor. Toda troca de mensagens é delegada ao
`MessageBroker`, que implementa a estratégia **publish/subscribe** (modelo
tópico-por-usuário): um cliente **assina** o próprio tópico para receber e
**publica** no tópico de um destinatário para enviar. O Broker decide, a cada
publicação, entre entrega instantânea (callback) e enfileiramento durável (MOM).

Além do roteamento, o servidor serve as consultas da UI: **histórico permanente**
de todas as mensagens e o **status de leitura** confirmado pelo cliente (ACK).
`ack_read` marca a mensagem como lida e a remove da fila; `get_history`/
`get_sent`/`get_conversation` alimentam as janelas de histórico. Ver §12.

Identidade dos clientes é **case-insensitive**: 'Pedro', 'pedro' e 'PEDRO'
referem-se ao mesmo cliente (e ao mesmo tópico). A forma canônica (minúsculas) é
a chave de presença, de assinatura e de fila.

Os nomes `register`/`send_message` são preservados como **aliases** de
`subscribe`/`publish` para compatibilidade com clientes já existentes.
"""
from __future__ import annotations

import Pyro5.api

from common import config
from common.models import Message, canonical_name, validate_contact_name
from server.broker import MessageBroker
from server.mom import MessageQueueManager
from server.presence import PresenceRegistry


@Pyro5.api.expose
class MessageServer:
    def __init__(
        self,
        broker: MessageBroker,
        mom: MessageQueueManager,
        presence: PresenceRegistry,
    ) -> None:
        self._broker = broker
        self._mom = mom
        self._presence = presence

    # ------------------------------------------------------------------ #
    # Unicidade de nome (case-insensitive)
    # ------------------------------------------------------------------ #
    def _name_in_use(self, key: str) -> bool:
        """True se há um cliente ONLINE e ACESSÍVEL com essa identidade.

        Faz um ping no callback registrado: se a presença for de uma sessão
        morta (cliente caiu sem se desconectar), libera o nome para reuso.
        """
        uri = self._presence.get_callback_uri(key)
        if uri is None:
            return False
        try:
            with Pyro5.api.Proxy(uri) as cb:
                cb._pyroTimeout = config.COMM_TIMEOUT
                cb.ping()
            return True
        except Exception:
            self._broker.unsubscribe(key)
            return False

    def is_name_available(self, contact_name: str) -> bool:
        """Pré-checagem usada pela UI antes de entrar (não reserva o nome)."""
        return not self._name_in_use(canonical_name(contact_name))

    # ------------------------------------------------------------------ #
    # Requisito 7: ao entrar, o cliente ASSINA seu próprio tópico.
    # ------------------------------------------------------------------ #
    def subscribe(self, contact_name: str, callback_uri: str) -> list[dict]:
        """Critica o nome, garante unicidade, assina o tópico e devolve pendentes.

        Assinar o próprio tópico (nome canônico) é o que habilita o cliente a
        receber publicações destinadas a ele. Levanta ValueError se o nome for
        inválido ou já estiver em uso por outro cliente conectado.
        """
        name = validate_contact_name(contact_name)  # critica o formato
        key = canonical_name(name)
        if self._name_in_use(key):
            raise ValueError(
                f"Já existe um cliente conectado com o nome '{name}'. "
                "Escolha outro nome."
            )
        pending = self._broker.subscribe(key, callback_uri)
        return [m.to_dict() for m in pending]

    #: Alias de compatibilidade (o antigo "register" agora é uma assinatura).
    register = subscribe

    def unsubscribe(self, contact_name: str) -> None:
        """Cliente saindo do sistema; cancela assinaturas (a fila permanece)."""
        self._broker.remove_subscriber(canonical_name(contact_name))
        print(f"[unsubscribe] {contact_name} saiu")

    #: Alias de compatibilidade.
    unregister = unsubscribe

    # ------------------------------------------------------------------ #
    # Requisito 2: alternar online/offline = assinar/cancelar o tópico.
    # ------------------------------------------------------------------ #
    def set_status(
        self, contact_name: str, online: bool, callback_uri: str | None = None
    ) -> list[dict]:
        """Muda o estado do cliente.

        ONLINE  -> assina o tópico e faz flush da fila (retorna as mensagens).
        OFFLINE -> cancela a assinatura; publicações futuras vão para a fila.
        """
        key = canonical_name(contact_name)
        if online:
            if not callback_uri:
                raise ValueError("callback_uri é obrigatório para ficar ONLINE")
            if self._name_in_use(key):
                raise ValueError(
                    f"Já existe um cliente conectado com o nome '{contact_name}'."
                )
            pending = self._broker.subscribe(key, callback_uri)
            return [m.to_dict() for m in pending]
        else:
            self._broker.unsubscribe(key)
            print(f"[status] {contact_name} OFFLINE")
            return []

    # ------------------------------------------------------------------ #
    # Requisitos 3, 4, 6: PUBLICAÇÃO com decisão online/offline no Broker.
    # ------------------------------------------------------------------ #
    def publish(
        self, sender: str, recipient: str, body: str, timestamp: str | None = None
    ) -> str:
        """Publica no tópico do destinatário; o Broker decide a entrega.

        O `timestamp` é gerado pelo remetente (com seu fuso) e apenas propagado,
        para que ambos os lados exibam a mesma hora. Retorna "DELIVERED" (algum
        assinante recebeu na hora) ou "QUEUED" (ficou na fila durável).
        """
        rkey = canonical_name(recipient)
        # message.recipient guarda a identidade canônica (o tópico / fila).
        message = Message(sender=sender, recipient=rkey, body=body)
        if timestamp:
            message.timestamp = timestamp
        return self._broker.publish(message)

    #: Alias de compatibilidade (o antigo "send_message" agora é uma publicação).
    send_message = publish

    def reject_message(
        self, rejecter: str, original_sender: str, original_body: str = "",
        msg_id: str | None = None,
    ) -> None:
        """`rejecter` recusa uma mensagem; avisa o remetente original via Broker.

        Se `msg_id` for informado, a mensagem recusada sai da fila de `rejecter`
        (não será reentregue), mas permanece "não lida" no histórico — ele nunca
        a leu.
        """
        if msg_id:
            self._mom.reject(canonical_name(rejecter), msg_id)
        self._broker.publish_rejection(rejecter, original_sender, original_body)

    # ------------------------------------------------------------------ #
    # Consulta de presença para a UI.
    # ------------------------------------------------------------------ #
    def get_status(self, contact_name: str) -> str:
        return self._presence.status_of(canonical_name(contact_name)).value

    def get_statuses(self, names: list[str]) -> dict[str, str]:
        # Lookup canônico, mas devolve a chave pedida (o cliente mapeia de volta).
        return {n: self._presence.status_of(canonical_name(n)).value for n in names}

    def fetch_offline(self, contact_name: str) -> list[dict]:
        """Puxa manualmente a fila (fallback/polling), sem removê-la.

        Como no flush, as mensagens só saem da fila quando confirmadas por
        `ack_read` — aqui apenas as espiamos (peek).
        """
        return [m.to_dict() for m in self._mom.peek(canonical_name(contact_name))]

    # ------------------------------------------------------------------ #
    # Consulta de histórico de mensagens do cliente.
    # ------------------------------------------------------------------ #
    def get_history(self, contact_name: str) -> list[dict]:
        """Histórico das mensagens recebidas pelo cliente, com status de leitura.

        Cada item: {sender, recipient, body, timestamp, read}. ``read`` é True
        ("lida") quando o cliente já recebeu a mensagem pelo menos uma vez, e
        False ("não lida") quando ela ainda aguarda na fila (nunca recebida).
        """
        return self._mom.history_for(canonical_name(contact_name))

    def get_sent(self, contact_name: str) -> list[dict]:
        """Histórico das mensagens enviadas pelo cliente, com status de leitura.

        Cada item: {sender, recipient, body, timestamp, read, read_at}. ``read``
        é True ("lida") quando o destinatário já confirmou o recebimento (ACK);
        False ("não lida") enquanto a mensagem aguarda na fila do destinatário.
        """
        return self._mom.sent_for(canonical_name(contact_name))

    def get_conversation(self, contact_name: str) -> list[dict]:
        """Conversa completa do cliente (enviadas + recebidas) para a UI.

        Usado para reconstruir o painel de conversa ao reabrir o cliente, com o
        servidor como fonte de verdade. Ver `MessageQueueManager.conversation_for`.
        """
        return self._mom.conversation_for(canonical_name(contact_name))

    def ack_read(self, contact_name: str, msg_id: str) -> bool:
        """ACK do cliente: confirma que recebeu e exibiu `msg_id`.

        Marca a mensagem como lida no histórico e a remove da fila. Idempotente
        (tolera reentregas/ACKs repetidos da semântica at-least-once).
        """
        return self._mom.ack_read(canonical_name(contact_name), msg_id)
