"""ClientService — fachada do lado do cliente.

Encapsula:
  * o daemon Pyro5 e o ClientCallback (provedor de RMI);
  * o proxy para o MessageServer (consumidor de RMI);
  * a `inbox` thread-safe que liga o callback (thread do daemon) à UI.

Não conhece Tkinter — a UI consome a `inbox` e chama estes métodos.
"""
from __future__ import annotations

import queue
import threading

import Pyro5.api

from datetime import datetime

from common import config
from common.models import Message
from client.callback import ClientCallback


class ClientService:
    def __init__(self, contact_name: str) -> None:
        self.contact_name = contact_name
        #: Fila ponte entre a thread do daemon Pyro5 e a thread da UI.
        self.inbox: "queue.Queue[tuple]" = queue.Queue()

        self._daemon = Pyro5.api.Daemon(host=config.PYRO_HOST)
        self._callback = ClientCallback(self.inbox)
        self._callback_uri = str(self._daemon.register(self._callback))
        self._daemon_thread: threading.Thread | None = None

    # -- ciclo de vida do daemon (provedor de RMI) -------------------------
    def start_daemon(self) -> None:
        """Inicia o requestLoop do daemon em uma thread dedicada."""
        self._daemon_thread = threading.Thread(
            target=self._daemon.requestLoop, daemon=True, name="pyro-daemon"
        )
        self._daemon_thread.start()

    def _server(self) -> Pyro5.api.Proxy:
        proxy = Pyro5.api.Proxy(f"PYRONAME:{config.SERVER_NAME}")
        proxy._pyroTimeout = config.COMM_TIMEOUT
        return proxy

    @staticmethod
    def is_name_available(contact_name: str) -> bool:
        """Pré-checagem de unicidade (case-insensitive) antes de entrar."""
        proxy = Pyro5.api.Proxy(f"PYRONAME:{config.SERVER_NAME}")
        proxy._pyroTimeout = config.COMM_TIMEOUT
        with proxy as s:
            return s.is_name_available(contact_name)

    # -- operações remotas (consumidor de RMI) -----------------------------
    def register(self) -> list[Message]:
        """Requisito 7: entra no sistema (cria fila) e recebe pendentes."""
        with self._server() as s:
            pending = s.register(self.contact_name, self._callback_uri)
        return [Message.from_dict(m) for m in pending]

    def set_online(self) -> list[Message]:
        """Requisito 2: fica ONLINE e recebe o flush da fila."""
        with self._server() as s:
            pending = s.set_status(self.contact_name, True, self._callback_uri)
        return [Message.from_dict(m) for m in pending]

    def set_offline(self) -> None:
        with self._server() as s:
            s.set_status(self.contact_name, False, None)

    def send(self, recipient: str, body: str) -> tuple[str, str]:
        """Requisitos 3/6: envia; servidor decide entre DELIVERED e QUEUED.

        O timestamp é carimbado AQUI (no remetente), com o fuso local embutido,
        e propagado inalterado ao destinatário. Retorna (resultado, timestamp).
        """
        timestamp = datetime.now().astimezone().isoformat()
        with self._server() as s:
            result = s.send_message(self.contact_name, recipient, body, timestamp)
        return result, timestamp

    def reject(self, original_sender: str, original_body: str = "",
               msg_id: str | None = None) -> None:
        """Avisa o servidor que recusamos a mensagem de `original_sender`.

        O servidor encaminha a notificação de rejeição ao remetente original e,
        se `msg_id` for dado, remove a mensagem recusada da nossa fila.
        """
        with self._server() as s:
            s.reject_message(self.contact_name, original_sender, original_body, msg_id)

    def ack_read(self, msg_id: str) -> None:
        """Confirma ao servidor que recebemos e exibimos a mensagem `msg_id`.

        Disparado pela UI **após** renderizar a mensagem, fechando a janela entre
        desenfileirar e exibir: a mensagem só sai da fila do servidor agora.
        """
        with self._server() as s:
            s.ack_read(self.contact_name, msg_id)

    def history(self) -> list[dict]:
        """Consulta no servidor o histórico de mensagens recebidas por este cliente.

        Cada item: {sender, recipient, body, timestamp, read}. Ver
        `MessageServer.get_history`.
        """
        with self._server() as s:
            return s.get_history(self.contact_name)

    def sent(self) -> list[dict]:
        """Consulta no servidor o histórico de mensagens ENVIADAS por este cliente.

        Cada item: {sender, recipient, body, timestamp, read, read_at}, onde
        `read` indica se o destinatário já leu. Ver `MessageServer.get_sent`.
        """
        with self._server() as s:
            return s.get_sent(self.contact_name)

    def conversation(self) -> list[dict]:
        """Conversa completa (enviadas + recebidas) para reconstruir o painel.

        Cada item: {msg_id, peer, direction, sender, body, timestamp, read}. Ver
        `MessageServer.get_conversation`.
        """
        with self._server() as s:
            return s.get_conversation(self.contact_name)

    def statuses(self, names: list[str]) -> dict[str, str]:
        if not names:
            return {}
        with self._server() as s:
            return s.get_statuses(names)

    def shutdown(self) -> None:
        try:
            with self._server() as s:
                s.unregister(self.contact_name)
        except Exception:
            pass
        self._daemon.shutdown()
