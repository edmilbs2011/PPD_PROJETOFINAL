"""MOM — Middleware Orientado a Mensagens.

Implementa uma **fila FIFO por cliente**, persistida em SQLite para que as
mensagens deixadas a destinatários offline sobrevivam ao reinício do servidor.

Há duas responsabilidades de armazenamento, deliberadamente separadas:

* ``messages`` — a **fila** propriamente dita. Persiste **apenas** as mensagens
  ainda pendentes (encaminhadas àquela seção/fila do servidor e ainda não
  entregues). Uma mensagem sai daqui assim que é entregue ao destinatário.
* ``message_log`` — o **histórico permanente**. Armazena **todas** as mensagens
  que passaram pelo servidor e nunca é esvaziado. É a fonte da consulta de
  histórico do cliente.

O **status de leitura** é uma confirmação explícita (ACK) do cliente, gravada
na coluna ``message_log.read_at``: ``NULL`` enquanto "não lida"; preenchida com
o instante da confirmação quando o cliente **renderiza** a mensagem na UI e
chama ``ack_read``. No mesmo ato, a mensagem é removida da fila. Isso dá
entrega **at-least-once com confirmação**: a mensagem só sai da fila quando há
prova de que o cliente a recebeu e exibiu — uma queda entre desenfileirar e
exibir não a perde, pois ela permanece na fila até o ACK chegar.

Tabelas:
    queues(name TEXT PRIMARY KEY, created_at TEXT)
    messages(seq, msg_id, queue_name, sender, recipient, body, timestamp)
    message_log(seq, msg_id, sender, recipient, body, timestamp, read_at)

O acesso é protegido por um `RLock`, pois o daemon Pyro5 pode atender
requisições concorrentes.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from common.models import Message, canonical_name


class MessageQueueManager:
    """Gerenciador de filas persistentes (o "broker" do MOM)."""

    def __init__(self, db_path: Path) -> None:
        self._lock = threading.RLock()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: acessado por várias threads do daemon, mas
        # serializado pelo nosso próprio lock.
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS queues (
                    name        TEXT PRIMARY KEY,
                    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE TABLE IF NOT EXISTS messages (
                    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
                    msg_id      TEXT NOT NULL,
                    queue_name  TEXT NOT NULL,
                    sender      TEXT NOT NULL,
                    recipient   TEXT NOT NULL,
                    body        TEXT NOT NULL,
                    timestamp   TEXT NOT NULL,
                    FOREIGN KEY (queue_name) REFERENCES queues(name)
                );
                CREATE INDEX IF NOT EXISTS idx_messages_queue
                    ON messages(queue_name, seq);
                CREATE TABLE IF NOT EXISTS message_log (
                    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
                    msg_id      TEXT NOT NULL UNIQUE,
                    sender      TEXT NOT NULL,
                    recipient   TEXT NOT NULL,
                    body        TEXT NOT NULL,
                    timestamp   TEXT NOT NULL,
                    read_at     TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_log_recipient
                    ON message_log(recipient, seq);
                """
            )
            # Migração leve: bancos criados antes do ACK não têm `read_at`.
            cols = {
                r["name"]
                for r in self._conn.execute("PRAGMA table_info(message_log)")
            }
            if "read_at" not in cols:
                self._conn.execute(
                    "ALTER TABLE message_log ADD COLUMN read_at TEXT"
                )

    # -- Requisito 7: criação da fila ao entrar no sistema ------------------
    def create_queue(self, name: str) -> bool:
        """Cria a fila do cliente (idempotente). Retorna True se criou agora."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO queues(name) VALUES (?)", (name,)
            )
            return cur.rowcount > 0

    def has_queue(self, name: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM queues WHERE name = ?", (name,)
            ).fetchone()
            return row is not None

    # -- Requisito 6: enfileirar mensagem para destinatário offline ---------
    def enqueue(self, message: Message) -> None:
        """Adiciona uma mensagem ao fim da fila do destinatário."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO queues(name) VALUES (?)",
                (message.recipient,),
            )
            self._conn.execute(
                """INSERT INTO messages
                       (msg_id, queue_name, sender, recipient, body, timestamp)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    message.msg_id,
                    message.recipient,
                    message.sender,
                    message.recipient,
                    message.body,
                    message.timestamp,
                ),
            )

    # -- Histórico permanente: registra TODAS as mensagens -----------------
    def log_message(self, message: Message) -> None:
        """Grava a mensagem no histórico permanente (idempotente por msg_id).

        Chamado para toda mensagem roteada pelo servidor, tenha ela sido
        entregue na hora ou enfileirada. O histórico nunca é apagado: é a base
        da consulta de mensagens do cliente. O status de leitura não é gravado
        aqui — ele é derivado da presença (ou ausência) da mensagem na fila.
        """
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT OR IGNORE INTO message_log
                       (msg_id, sender, recipient, body, timestamp)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    message.msg_id,
                    message.sender,
                    message.recipient,
                    message.body,
                    message.timestamp,
                ),
            )

    def history_for(self, name: str) -> list[dict]:
        """Histórico das mensagens recebidas por `name`, em ordem cronológica.

        Cada item traz remetente, conteúdo, timestamp, `read` (booleano) e
        `read_at`. ``read=True`` ("lida") quando há ACK do cliente confirmando
        que recebeu e exibiu a mensagem; ``read=False`` ("não lida") quando o
        cliente nunca confirmou (a mensagem segue pendente na fila).
        """
        with self._lock:
            rows = self._conn.execute(
                """SELECT msg_id, sender, recipient, body, timestamp, read_at
                       FROM message_log
                       WHERE recipient = ?
                       ORDER BY seq ASC""",
                (name,),
            ).fetchall()
        return [
            {
                "msg_id": r["msg_id"],
                "sender": r["sender"],
                "recipient": r["recipient"],
                "body": r["body"],
                "timestamp": r["timestamp"],
                "read": r["read_at"] is not None,
                "read_at": r["read_at"],
            }
            for r in rows
        ]

    def sent_for(self, name: str) -> list[dict]:
        """Histórico das mensagens ENVIADAS por `name` (canônico), cronológico.

        Simétrico a `history_for`. Aqui `read` reflete se o **destinatário** já
        leu: `read_at` é gravado pelo ACK de quem recebeu. ``read=False`` ("não
        lida") significa que a mensagem ainda aguarda na fila do destinatário.
        """
        with self._lock:
            rows = self._conn.execute(
                """SELECT sender, recipient, body, timestamp, read_at
                       FROM message_log
                       WHERE lower(sender) = ?
                       ORDER BY seq ASC""",
                (name,),
            ).fetchall()
        return [
            {
                "sender": r["sender"],
                "recipient": r["recipient"],
                "body": r["body"],
                "timestamp": r["timestamp"],
                "read": r["read_at"] is not None,
                "read_at": r["read_at"],
            }
            for r in rows
        ]

    def conversation_for(self, name: str) -> list[dict]:
        """Conversa completa que envolve `name` (canônico), em ordem cronológica.

        Diferente de `history_for` (só recebidas), traz **as duas direções** —
        enviadas e recebidas — para reconstruir o painel de conversa do cliente
        ao reabrir o app. Cada item:
            peer      -> o outro participante (chave da conversa no cliente)
            direction -> "sent" (name é o remetente) | "received"
            sender    -> autor a exibir
            read      -> recebidas: se `name` leu; enviadas: se o peer leu
        """
        with self._lock:
            rows = self._conn.execute(
                """SELECT msg_id, sender, recipient, body, timestamp, read_at
                       FROM message_log
                       WHERE recipient = ? OR lower(sender) = ?
                       ORDER BY seq ASC""",
                (name, name),
            ).fetchall()
        out: list[dict] = []
        for r in rows:
            is_sender = canonical_name(r["sender"]) == name
            out.append(
                {
                    "msg_id": r["msg_id"],
                    "peer": r["recipient"] if is_sender else r["sender"],
                    "direction": "sent" if is_sender else "received",
                    "sender": r["sender"],
                    "body": r["body"],
                    "timestamp": r["timestamp"],
                    "read": r["read_at"] is not None,
                }
            )
        return out

    # -- ACK de leitura: confirma entrega e remove da fila ------------------
    def ack_read(self, name: str, msg_id: str) -> bool:
        """Confirma que `name` recebeu e exibiu `msg_id`.

        Operação atômica e idempotente: marca `read_at` (apenas na primeira
        vez) e remove a mensagem da fila do cliente. Tolera ACKs repetidos
        (entrega at-least-once pode reentregar se um ACK anterior se perdeu).
        Retorna True se foi a confirmação que efetivamente marcou como lida.
        """
        now = datetime.now().astimezone().isoformat()
        with self._lock, self._conn:
            cur = self._conn.execute(
                """UPDATE message_log SET read_at = ?
                       WHERE msg_id = ? AND read_at IS NULL""",
                (now, msg_id),
            )
            # Sai da fila: já há prova de recebimento (pode não estar na fila se
            # foi entrega instantânea — nesse caso o DELETE é um no-op).
            self._conn.execute(
                "DELETE FROM messages WHERE queue_name = ? AND msg_id = ?",
                (name, msg_id),
            )
            return cur.rowcount > 0

    def reject(self, name: str, msg_id: str) -> None:
        """Remove da fila de `name` uma mensagem recusada (não-contato).

        Diferente do ACK, **não** marca `read_at`: a mensagem permanece "não
        lida" no histórico (o cliente nunca a leu), mas deixa de ser reentregue.
        """
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM messages WHERE queue_name = ? AND msg_id = ?",
                (name, msg_id),
            )

    # -- Requisitos 2 e 4: flush da fila quando o cliente fica online -------
    def peek(self, name: str) -> list[Message]:
        """Lê as mensagens pendentes em ordem FIFO **sem removê-las**.

        É a base do flush: ao ficar online, o cliente recebe estas mensagens,
        mas elas só saem da fila quando cada uma é confirmada por `ack_read`.
        Garante que nada se perca entre desenfileirar e exibir na UI.
        """
        with self._lock:
            rows = self._conn.execute(
                """SELECT msg_id, sender, recipient, body, timestamp
                       FROM messages WHERE queue_name = ? ORDER BY seq ASC""",
                (name,),
            ).fetchall()
        return [
            Message(r["sender"], r["recipient"], r["body"], r["timestamp"], r["msg_id"])
            for r in rows
        ]

    def count(self, name: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE queue_name = ?", (name,)
            ).fetchone()
            return int(row["n"])

    def delete_queue(self, name: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM messages WHERE queue_name = ?", (name,))
            self._conn.execute("DELETE FROM queues WHERE name = ?", (name,))

    def close(self) -> None:
        with self._lock:
            self._conn.close()
