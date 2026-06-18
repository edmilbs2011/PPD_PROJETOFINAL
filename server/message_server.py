"""Objeto remoto principal: MessageServer (interface §3.1 da ARQUITETURA.md).

Atua como diretório de presença e como broker de mensagens offline (MOM).
Decide, a cada envio, entre entrega instantânea (callback) e enfileiramento.

Também mantém o **histórico permanente** de todas as mensagens e o **status de
leitura** confirmado pelo cliente (ACK): `ack_read` marca a mensagem como lida e
a remove da fila; `get_history`/`get_sent`/`get_conversation` servem as consultas
da UI. Ver §12 da ARQUITETURA.md.

Identidade dos clientes é **case-insensitive**: 'Pedro', 'pedro' e 'PEDRO'
referem-se ao mesmo cliente. A forma canônica (minúsculas) é usada como chave
de presença e nome da fila no MOM.
"""
from __future__ import annotations

import Pyro5.api

from common import config
from common.models import Message, canonical_name, validate_contact_name
from server.mom import MessageQueueManager
from server.presence import PresenceRegistry


@Pyro5.api.expose
class MessageServer:
    def __init__(self, mom: MessageQueueManager, presence: PresenceRegistry) -> None:
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
            self._presence.set_offline(key)
            return False

    def is_name_available(self, contact_name: str) -> bool:
        """Pré-checagem usada pela UI antes de entrar (não reserva o nome)."""
        return not self._name_in_use(canonical_name(contact_name))

    # ------------------------------------------------------------------ #
    # Requisito 7: ao entrar, o cliente pede para criar sua fila.
    # ------------------------------------------------------------------ #
    def register(self, contact_name: str, callback_uri: str) -> list[dict]:
        """Critica o nome, garante unicidade, registra ONLINE e devolve pendentes.

        Levanta ValueError se o nome for inválido ou já estiver em uso por outro
        cliente conectado (comparação case-insensitive).
        """
        name = validate_contact_name(contact_name)  # critica o formato
        key = canonical_name(name)
        if self._name_in_use(key):
            raise ValueError(
                f"Já existe um cliente conectado com o nome '{name}'. "
                "Escolha outro nome."
            )
        self._mom.create_queue(key)
        self._presence.set_online(key, callback_uri)
        # Flush não-destrutivo: as pendentes só saem da fila quando confirmadas
        # por ack_read (entrega at-least-once com confirmação).
        pending = self._mom.peek(key)
        print(f"[register] {name} ({key}) ONLINE — {len(pending)} pendente(s)")
        return [m.to_dict() for m in pending]

    def unregister(self, contact_name: str) -> None:
        """Cliente saindo do sistema; libera a presença (a fila permanece)."""
        key = canonical_name(contact_name)
        self._presence.remove(key)
        print(f"[unregister] {contact_name} saiu")

    # ------------------------------------------------------------------ #
    # Requisito 2: alternar online/offline.
    # ------------------------------------------------------------------ #
    def set_status(
        self, contact_name: str, online: bool, callback_uri: str | None = None
    ) -> list[dict]:
        """Muda o estado do cliente.

        ONLINE  -> registra a URI e faz flush da fila (retorna as mensagens).
        OFFLINE -> remove a URI; envios futuros vão para a fila.
        """
        key = canonical_name(contact_name)
        if online:
            if not callback_uri:
                raise ValueError("callback_uri é obrigatório para ficar ONLINE")
            if self._name_in_use(key):
                raise ValueError(
                    f"Já existe um cliente conectado com o nome '{contact_name}'."
                )
            self._mom.create_queue(key)
            self._presence.set_online(key, callback_uri)
            pending = self._mom.peek(key)  # removidas só após ack_read
            print(f"[status] {contact_name} ONLINE — flush de {len(pending)}")
            return [m.to_dict() for m in pending]
        else:
            self._presence.set_offline(key)
            print(f"[status] {contact_name} OFFLINE")
            return []

    # ------------------------------------------------------------------ #
    # Requisitos 3, 4, 6: envio com decisão online/offline.
    # ------------------------------------------------------------------ #
    def send_message(
        self, sender: str, recipient: str, body: str, timestamp: str | None = None
    ) -> str:
        """Entrega instantânea se o destinatário estiver online; senão, enfileira.

        O `timestamp` é gerado pelo remetente (com seu fuso) e apenas propagado,
        para que ambos os lados exibam a mesma hora. Retorna "DELIVERED" ou "QUEUED".
        """
        rkey = canonical_name(recipient)
        # message.recipient guarda a identidade canônica (chave da fila no MOM).
        message = Message(sender=sender, recipient=rkey, body=body)
        if timestamp:
            message.timestamp = timestamp

        # Histórico permanente: registra TODA mensagem como "não lida" (read_at
        # NULL). Só o ACK do cliente (ack_read) a marca como lida.
        self._mom.log_message(message)

        # Copia a URI sob lock e invoca o callback FORA do lock (evita travar o
        # registro durante uma chamada de rede potencialmente lenta).
        uri = self._presence.get_callback_uri(rkey)
        if uri is not None:
            # Entrega instantânea, mas NÃO é tratada como lida aqui: o cliente
            # confirma com ack_read após renderizar. Por isso enfileiramos
            # também — a mensagem só sai da fila no ACK (se o push se perder
            # entre receber e exibir, ela é reentregue no próximo login).
            self._mom.enqueue(message)
            try:
                with Pyro5.api.Proxy(uri) as cb:
                    cb.receive_message(
                        message.sender, message.body, message.timestamp, message.msg_id
                    )
                print(f"[send] {sender} -> {recipient}: DELIVERED")
                return "DELIVERED"
            except Exception as exc:  # destinatário inacessível: degrada p/ offline
                print(f"[send] falha no push p/ {recipient} ({exc!r}); enfileirando")
                self._presence.set_offline(rkey)
                print(f"[send] {sender} -> {recipient}: QUEUED ({self._mom.count(rkey)} na fila)")
                return "QUEUED"

        # Destinatário offline: vai para a fila do destinatário.
        self._mom.enqueue(message)
        print(f"[send] {sender} -> {recipient}: QUEUED ({self._mom.count(rkey)} na fila)")
        return "QUEUED"

    def reject_message(
        self, rejecter: str, original_sender: str, original_body: str = "",
        msg_id: str | None = None,
    ) -> None:
        """Encaminha ao `original_sender` o aviso de que `rejecter` recusou a mensagem.

        Se o remetente original estiver online, faz push do aviso; se estiver
        offline, enfileira como mensagem comum para não se perder.

        Se `msg_id` for informado, a mensagem recusada sai da fila de `rejecter`
        (não será reentregue), mas permanece "não lida" no histórico — ele nunca
        a leu.
        """
        if msg_id:
            self._mom.reject(canonical_name(rejecter), msg_id)
        skey = canonical_name(original_sender)
        uri = self._presence.get_callback_uri(skey)
        if uri is not None:
            try:
                with Pyro5.api.Proxy(uri) as cb:
                    cb.notify_rejection(rejecter, original_body)
                print(f"[reject] {rejecter} recusou msg de {original_sender}")
                return
            except Exception:
                self._presence.set_offline(skey)
        # Remetente offline: deixa um aviso na fila dele.
        notice = Message(
            sender=rejecter, recipient=skey,
            body=f"(rejeitou sua mensagem) {original_body}".strip(),
        )
        self._mom.log_message(notice)
        self._mom.enqueue(notice)
        print(f"[reject] {rejecter} recusou msg de {original_sender} (enfileirado)")

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
