"""ClientCallback — objeto remoto que o servidor invoca para entrega instantânea.

IMPORTANTE (concorrência): este callback roda na thread do daemon Pyro5, NÃO na
thread do Tkinter. Por isso ele apenas empurra os eventos para uma fila
thread-safe (`inbox`); a UI os consome na sua própria thread via `root.after()`.
"""
from __future__ import annotations

import queue

import Pyro5.api


@Pyro5.api.expose
class ClientCallback:
    def __init__(self, inbox: "queue.Queue[tuple]") -> None:
        self._inbox = inbox

    # Requisito 3: recebimento instantâneo quando online.
    def receive_message(
        self, sender: str, body: str, timestamp: str, msg_id: str | None = None
    ) -> None:
        # O ACK de leitura só é enviado pela UI, após renderizar a mensagem.
        self._inbox.put(("message", sender, body, timestamp, msg_id))

    def notify_status(self, contact_name: str, status: str) -> None:
        """(Opcional) notificação de mudança de presença de um contato."""
        self._inbox.put(("status", contact_name, status))

    def notify_rejection(self, rejecter: str, original_body: str = "") -> None:
        """O destinatário `rejecter` recusou a mensagem deste cliente."""
        self._inbox.put(("rejection", rejecter, original_body))

    def ping(self) -> bool:
        return True
