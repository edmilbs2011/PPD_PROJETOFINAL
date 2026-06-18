"""ChatWindow — interface Tkinter.

Layout:
  +-----------------------------------------------------------------+
  | Você: <nome>   Histórico:[Recebidas][Enviadas]  [Online▢] (conn) |
  +------------------+----------------------------------------------+
  | Contatos         |  Conversa com <contato>                      |
  | ● bob            |  ...painel da conversa (enviadas/recebidas)..|
  | ○ carol          |                                              |
  | [+ add] [- del]  |  [ caixa de texto ........ ][Enviar]         |
  +------------------+----------------------------------------------+

Responsabilidades principais:
  * Painel de conversa por contato (`_history`), reconstruído do servidor no
    startup (`load_history`) — a conversa reabre preenchida entre sessões.
  * Envio (`_send`) e recebimento (`_receive`) de mensagens.
  * Confirmação de leitura (ACK): ao **exibir** uma mensagem recebida, `_ack`
    avisa o servidor, que a marca "lida" e a remove da fila. Reentregas
    (entrega at-least-once) são deduplicadas por `msg_id` (`_seen_msg_ids`).
  * Janelas de consulta de histórico (`Recebidas`/`Enviadas`), com botão
    Atualizar e — nas recebidas — conteúdo oculto até clicar no status "não lida".

Concorrência: o callback Pyro roda em outra thread e só empilha eventos na
`inbox` do ClientService; a UI a drena periodicamente com `root.after()`
(`_drain_inbox`), garantindo que widgets só sejam tocados na thread do Tkinter.
"""
from __future__ import annotations

import queue
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, simpledialog, ttk

from common.models import Message, Status
from client.contacts import ContactBook
from client.service import ClientService

_POLL_INBOX_MS = 100      # drenagem da inbox de callbacks
_POLL_STATUS_MS = 4000    # atualização de presença dos contatos


def _format_ts(iso_ts: str) -> str:
    """Converte um timestamp ISO-8601 em data/hora legível (dd/mm/aaaa HH:MM:SS)."""
    try:
        return datetime.fromisoformat(iso_ts).strftime("%d/%m/%Y %H:%M:%S")
    except (ValueError, TypeError):
        return iso_ts


def _split_ts(iso_ts: str) -> tuple[str, str]:
    """Separa um timestamp ISO-8601 em (data dd/mm/aaaa, hora HH:MM:SS)."""
    try:
        dt = datetime.fromisoformat(iso_ts)
        return dt.strftime("%d/%m/%Y"), dt.strftime("%H:%M:%S")
    except (ValueError, TypeError):
        return iso_ts, ""


class ChatWindow:
    def __init__(self, service: ClientService, contacts: ContactBook) -> None:
        self.service = service
        self.contacts = contacts
        self.online = False
        #: painel da conversa por contato -> lista de registros
        #: (kind, autor, timestamp, corpo); kind ∈ {"sent", "received", "system"}
        #: ("system" = avisos locais, p.ex. notificação de rejeição).
        self._history: dict[str, list[tuple[str, str, str, str]]] = {}
        #: último status conhecido de cada contato (para a UI)
        self._statuses: dict[str, str] = {}
        self._current: str | None = None
        #: BooleanVar de cada mensagem exibida (paralelo ao histórico do contato),
        #: usado pelas caixas de seleção para excluir mensagens.
        self._msg_vars: list[tk.BooleanVar] = []
        #: msg_ids já exibidos nesta sessão — deduplica reentregas (at-least-once),
        #: evitando renderizar duas vezes a mesma mensagem se um ACK se perdeu.
        self._seen_msg_ids: set[str] = set()

        self.root = tk.Tk()
        self.root.title(f"PPD Chat — {service.contact_name}")
        self.root.geometry("720x460")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_layout()
        self._refresh_contacts()

        # Laços periódicos.
        self.root.after(_POLL_INBOX_MS, self._drain_inbox)
        self.root.after(500, self._poll_statuses)

    # ------------------------------------------------------------------ #
    # Construção do layout
    # ------------------------------------------------------------------ #
    def _build_layout(self) -> None:
        top = ttk.Frame(self.root, padding=6)
        top.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(top, text=f"Você: {self.service.contact_name}",
                  font=("TkDefaultFont", 10, "bold")).pack(side=tk.LEFT)
        ttk.Label(top, text="Histórico:").pack(side=tk.LEFT, padx=(12, 2))
        ttk.Button(top, text="Recebidas",
                   command=self._show_received_history).pack(side=tk.LEFT, padx=2)
        ttk.Button(top, text="Enviadas",
                   command=self._show_sent_history).pack(side=tk.LEFT, padx=2)
        self.online_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Online", variable=self.online_var,
                        command=self._toggle_online).pack(side=tk.RIGHT)
        self.conn_lbl = ttk.Label(top, text="● offline", foreground="gray")
        self.conn_lbl.pack(side=tk.RIGHT, padx=8)

        body = ttk.Frame(self.root)
        body.pack(fill=tk.BOTH, expand=True)

        # --- painel esquerdo: lista de contatos (sempre visível) ---------
        left = ttk.Frame(body, padding=6)
        left.pack(side=tk.LEFT, fill=tk.Y)
        ttk.Label(left, text="Contatos").pack(anchor=tk.W)
        self.contact_list = tk.Listbox(left, width=22, height=18)
        self.contact_list.pack(fill=tk.Y, expand=True)
        self.contact_list.bind("<<ListboxSelect>>", self._on_select_contact)
        btns = ttk.Frame(left)
        btns.pack(fill=tk.X, pady=4)
        ttk.Button(btns, text="+ Adicionar", command=self._add_contact).pack(side=tk.LEFT, expand=True, fill=tk.X)
        ttk.Button(btns, text="- Remover", command=self._remove_contact).pack(side=tk.LEFT, expand=True, fill=tk.X)

        # --- painel direito: conversa ------------------------------------
        right = ttk.Frame(body, padding=6)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        title_row = ttk.Frame(right)
        title_row.pack(fill=tk.X)
        self.chat_title = ttk.Label(title_row, text="Selecione um contato",
                                    font=("TkDefaultFont", 10, "bold"))
        self.chat_title.pack(side=tk.LEFT)
        #: status textual do contato selecionado (online/offline)
        self.chat_status = ttk.Label(title_row, text="", foreground="gray")
        self.chat_status.pack(side=tk.LEFT, padx=6)
        ttk.Button(title_row, text="Excluir selecionadas",
                   command=self._delete_selected).pack(side=tk.RIGHT)
        ttk.Button(title_row, text="Marcar todas",
                   command=self._toggle_select_all).pack(side=tk.RIGHT, padx=4)
        # Cada mensagem é exibida com uma caixa de seleção (Checkbutton) embutida.
        self.chat_view = tk.Text(right, wrap=tk.WORD)
        self.chat_view.pack(fill=tk.BOTH, expand=True, pady=4)
        # Somente-leitura para o texto: bloqueia digitação, mantém copiar.
        self.chat_view.bind("<Key>", self._block_edit)
        # Cores/estilos por tipo de mensagem:
        #   enviada  -> azul, negrito
        #   recebida -> verde-escuro, normal
        self.chat_view.tag_configure(
            "sent", foreground="#1565C0", font=("TkDefaultFont", 10, "bold"))
        self.chat_view.tag_configure(
            "received", foreground="#19491B", font=("TkDefaultFont", 10))
        #   sistema (ex.: aviso de rejeição) -> cinza, itálico
        self.chat_view.tag_configure(
            "system", foreground="#9E9E9E", font=("TkDefaultFont", 9, "italic"))
        entry_row = ttk.Frame(right)
        entry_row.pack(fill=tk.X)
        self.entry = ttk.Entry(entry_row)
        self.entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.entry.bind("<Return>", lambda _e: self._send())
        ttk.Button(entry_row, text="Enviar", command=self._send).pack(side=tk.LEFT, padx=4)

    # ------------------------------------------------------------------ #
    # Contatos (Requisitos 1 e 8)
    # ------------------------------------------------------------------ #
    def _refresh_contacts(self) -> None:
        sel = self._current
        self.contact_list.delete(0, tk.END)
        for name in self.contacts.all():
            status = self._statuses.get(name, Status.OFFLINE.value)
            dot = "●" if status == Status.ONLINE.value else "○"
            label = "online" if status == Status.ONLINE.value else "offline"
            self.contact_list.insert(tk.END, f"{dot} {name}  ({label})")
        # Reseleciona o contato atual, se ainda existir.
        if sel and sel in self.contacts.all():
            idx = self.contacts.all().index(sel)
            self.contact_list.selection_set(idx)

    def _add_contact(self) -> None:
        name = simpledialog.askstring("Adicionar contato", "Nome do contato:", parent=self.root)
        if not name:
            return
        if name.strip().upper() == self.service.contact_name.strip().upper():
            messagebox.showwarning(
                "Contato", "Você não pode adicionar a si mesmo aos contatos.")
            return
        if self.contacts.add(name):
            self._refresh_contacts()
        else:
            messagebox.showinfo("Contato", "Contato já existe ou nome inválido.")

    def _remove_contact(self) -> None:
        name = self._selected_contact()
        if not name:
            return
        if self.contacts.remove(name):
            if self._current == name:
                self._current = None
                self.chat_title.config(text="Selecione um contato")
                self.chat_status.config(text="")
                self._render_history()
            self._refresh_contacts()

    def _selected_contact(self) -> str | None:
        sel = self.contact_list.curselection()
        if not sel:
            return None
        names = self.contacts.all()  # mesma ordem da Listbox
        idx = sel[0]
        return names[idx] if 0 <= idx < len(names) else None

    def _on_select_contact(self, _event=None) -> None:
        name = self._selected_contact()
        if name:
            self._current = name
            self.chat_title.config(text=f"Conversa com {name}")
            self._update_chat_status()
            self._render_history()

    def _update_chat_status(self) -> None:
        """Atualiza o rótulo 'online/offline' do contato em conversa."""
        if not self._current:
            self.chat_status.config(text="")
            return
        status = self._statuses.get(self._current, Status.UNKNOWN.value)
        if status == Status.ONLINE.value:
            self.chat_status.config(text="(online)", foreground="green")
        elif status == Status.OFFLINE.value:
            self.chat_status.config(text="(offline)", foreground="gray")
        else:
            self.chat_status.config(text="(desconhecido)", foreground="gray")

    # ------------------------------------------------------------------ #
    # Online/offline (Requisito 2)
    # ------------------------------------------------------------------ #
    def _toggle_online(self) -> None:
        try:
            if self.online_var.get():
                pending = self.service.set_online()
                self.online = True
                self.conn_lbl.config(text="● online", foreground="green")
                for m in pending:
                    self._receive(m, queued=True)
            else:
                self.service.set_offline()
                self.online = False
                self.conn_lbl.config(text="● offline", foreground="gray")
        except Exception as exc:
            self.online_var.set(self.online)
            messagebox.showerror("Servidor", f"Falha ao mudar estado:\n{exc}")

    # ------------------------------------------------------------------ #
    # Envio / recebimento (Requisitos 3, 4, 6)
    # ------------------------------------------------------------------ #
    def _send(self) -> None:
        recipient = self._current
        body = self.entry.get().strip()
        if not recipient:
            messagebox.showinfo("Enviar", "Selecione um contato primeiro.")
            return
        if not body:
            return
        try:
            result, timestamp = self.service.send(recipient, body)
        except Exception as exc:
            messagebox.showerror("Servidor", f"Falha ao enviar:\n{exc}")
            return
        suffix = "" if result == "DELIVERED" else "  (offline → fila)"
        # Mostra o mesmo timestamp carimbado no envio (mesma hora que o destinatário verá).
        # Mensagem enviada (negrito/azul): "<eu>: data/hora - corpo".
        self._append_history(recipient, "sent", self.service.contact_name,
                             _format_ts(timestamp), body + suffix)
        self.entry.delete(0, tk.END)

    def _receive(self, message: Message, queued: bool = False) -> None:
        mid = message.msg_id
        # Deduplicação (entrega at-least-once): se já exibimos esta mensagem,
        # não a renderiza de novo. Reenvia o ACK por garantia — caso o anterior
        # tenha se perdido — para que ela saia da fila do servidor.
        if mid and mid in self._seen_msg_ids:
            self._ack(mid)
            return

        sender = (message.sender or "").strip().upper()

        # Mensagem de quem NÃO está nos contatos: pergunta se aceita.
        if sender and sender not in self.contacts:
            accept = messagebox.askyesno(
                "Mensagem de não-contato",
                "Você recebeu uma mensagem de alguém que não está em seus "
                f"contatos.\n\nRemetente: {message.sender}\n\nAceita?\n\n"
                "Se aceitar, o remetente é incluído na sua lista de contatos e "
                "a mensagem é exibida. Se recusar, a mensagem é descartada e o "
                "remetente é avisado da rejeição.",
            )
            if accept:
                self.contacts.add(message.sender)
                self._refresh_contacts()
            else:
                # Recusada: NÃO confirma leitura. Avisa o remetente e pede ao
                # servidor para tirá-la da fila (continua "não lida" no histórico).
                if mid:
                    self._seen_msg_ids.add(mid)
                try:
                    self.service.reject(message.sender, message.body, mid)
                except Exception as exc:
                    messagebox.showerror(
                        "Servidor", f"Falha ao notificar rejeição:\n{exc}")
                return

        prefix = "[offline] " if queued else ""
        ts = _format_ts(message.timestamp)
        # Mensagem recebida (normal/verde): "<remetente>: data/hora - corpo".
        self._append_history(message.sender, "received", message.sender, ts, prefix + message.body)
        # Exibida na UI: agora sim confirma a leitura ao servidor (ACK), que a
        # marca como "lida" e a remove da fila.
        if mid:
            self._seen_msg_ids.add(mid)
            self._ack(mid)

    def _ack(self, msg_id: str) -> None:
        """Confirma a leitura ao servidor (best-effort).

        Uma falha aqui não é fatal: sem o ACK, a mensagem permanece na fila e é
        reentregue no próximo login, quando a deduplicação evita exibi-la de novo.
        """
        try:
            self.service.ack_read(msg_id)
        except Exception:
            pass

    def load_history(self) -> None:
        """Reconstrói o painel de conversa a partir do servidor (fonte de verdade).

        Chamado uma vez no startup — é o que faz a conversa reabrir já preenchida.
        Carrega as mensagens ENVIADAS e as RECEBIDAS já lidas. As recebidas ainda
        pendentes ("não lidas") são deixadas para o fluxo normal de entrega (flush),
        que cuida do diálogo de não-contato e do ACK; assim nada é exibido sem a
        decisão de aceite. Deduplica naturalmente: a fonte é o log (chave `msg_id`).
        """
        try:
            records = self.service.conversation()
        except Exception:
            return  # sem servidor: começa vazio (comportamento anterior)
        for rec in records:
            peer = (rec.get("peer") or "").strip().upper()
            if not peer:
                continue
            ts = _format_ts(rec.get("timestamp", ""))
            body = rec.get("body", "")
            if rec.get("direction") == "sent":
                self._history.setdefault(peer, []).append(
                    ("sent", self.service.contact_name, ts, body))
            elif rec.get("read"):  # recebida e já lida: já decidida/exibida antes
                self._history.setdefault(peer, []).append(
                    ("received", rec.get("sender", ""), ts, body))
                mid = rec.get("msg_id")
                if mid:
                    self._seen_msg_ids.add(mid)
            # recebida pendente -> ignorada aqui; chega pelo flush e passa por _receive
        if self._current:
            self._render_history()

    def _show_rejection(self, rejecter: str, original_body: str) -> None:
        """Exibe, na conversa com `rejecter`, o aviso de que ele rejeitou a mensagem."""
        note = f"{rejecter} rejeitou sua mensagem."
        if original_body:
            note += f' ("{original_body}")'
        ts = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        self._append_history(rejecter, "system", "", ts, note)

    # ------------------------------------------------------------------ #
    # Histórico
    # ------------------------------------------------------------------ #
    def _append_history(self, contact: str, kind: str, author: str,
                        timestamp: str, body: str) -> None:
        # A conversa é indexada pela identidade em MAIÚSCULAS, igual aos contatos,
        # para que enviadas e recebidas caiam sempre na mesma conversa (sem
        # distinção de maiúsculas/minúsculas no remetente).
        contact = contact.strip().upper()
        self._history.setdefault(contact, []).append((kind, author, timestamp, body))
        if contact == self._current:
            self._render_history()

    def _render_history(self) -> None:
        """Renderiza o histórico do contato selecionado com as cores por tipo.

        Cada mensagem recebe uma caixa de seleção (Checkbutton) embutida no início
        da linha; `self._msg_vars[i]` corresponde à mensagem `i` do histórico.
        Formato: "[ ] Nome do remetente: data/hora - Conteúdo da mensagem".
        """
        self.chat_view.delete("1.0", tk.END)  # destrói também os checkbuttons antigos
        self._msg_vars = []
        bg = self.chat_view.cget("background")
        for kind, author, timestamp, body in self._history.get(self._current or "", []):
            var = tk.BooleanVar(value=False)
            self._msg_vars.append(var)
            chk = tk.Checkbutton(
                self.chat_view, variable=var, bg=bg, activebackground=bg,
                takefocus=False, borderwidth=0, highlightthickness=0, padx=0, pady=0)
            self.chat_view.window_create(tk.END, window=chk)
            if kind == "system":
                self.chat_view.insert(tk.END, f" ⚠ {timestamp} - {body}\n", kind)
            else:
                self.chat_view.insert(tk.END, f" {author}: {timestamp} - {body}\n", kind)
        self.chat_view.see(tk.END)

    def _toggle_select_all(self) -> None:
        """Marca todas as caixas; se já estiverem todas marcadas, desmarca todas."""
        if not self._msg_vars:
            return
        target = not all(v.get() for v in self._msg_vars)
        for v in self._msg_vars:
            v.set(target)

    # ------------------------------------------------------------------ #
    # Exclusão de mensagens selecionadas
    # ------------------------------------------------------------------ #
    @staticmethod
    def _block_edit(event):
        """Mantém o chat somente-leitura, mas permite copiar (Ctrl+C/Ctrl+A)."""
        if event.state & 0x4 and event.keysym.lower() in ("c", "a"):
            return None
        return "break"

    def _delete_selected(self) -> None:
        """Exclui do histórico as mensagens com a caixa de seleção marcada."""
        if not self._current:
            return
        selected = [i for i, v in enumerate(self._msg_vars) if v.get()]
        if not selected:
            messagebox.showinfo(
                "Excluir", "Marque a caixa das mensagens que deseja excluir.")
            return
        if not messagebox.askyesno(
            "Excluir", f"Excluir {len(selected)} mensagem(ns) marcada(s)?"
        ):
            return
        history = self._history.get(self._current, [])
        # Remove de trás para frente para os índices não se deslocarem.
        for i in sorted(selected, reverse=True):
            if 0 <= i < len(history):
                del history[i]
        self._render_history()

    # ------------------------------------------------------------------ #
    # Consulta do histórico de mensagens (data, hora, contato, conteúdo, status)
    # ------------------------------------------------------------------ #
    def _show_received_history(self) -> None:
        """Histórico de mensagens RECEBIDAS: Data | Hora | Remetente | Msg | Status.

        As mensagens "não lidas" têm o conteúdo OCULTO; ao clicar no status
        'não lida', o conteúdo é revelado, o status passa a 'lida' e a mudança é
        gravada no banco (via ACK).
        """
        self._open_history_window(
            title="Histórico de mensagens recebidas",
            peer_heading="Remetente",
            peer_field="sender",
            fetch=self.service.history,
            help_text=("Mensagens recebidas por você. As 'não lidas' aparecem "
                       "ocultas — clique no status 'não lida' para ler (ela passa "
                       "a 'lida')."),
            revealable=True,
        )

    def _show_sent_history(self) -> None:
        """Histórico de mensagens ENVIADAS: Data | Hora | Destinatário | Msg | Status.

        Status "lida" = o destinatário já recebeu/leu; "não lida" = ainda na fila
        dele (ele nunca recebeu).
        """
        self._open_history_window(
            title="Histórico de mensagens enviadas",
            peer_heading="Destinatário",
            peer_field="recipient",
            fetch=self.service.sent,
            help_text=("Mensagens enviadas por você. Status: 'lida' = o destinatário "
                       "já recebeu/leu; 'não lida' = ainda na fila dele (não recebida)."),
        )

    #: Texto exibido no lugar do conteúdo de uma mensagem ainda não lida.
    _HIDDEN_BODY = "•••••••  (clique em 'não lida' para ler)"

    def _open_history_window(self, *, title: str, peer_heading: str,
                             peer_field: str, fetch, help_text: str,
                             revealable: bool = False) -> None:
        """Builder genérico das janelas de histórico (recebidas/enviadas).

        `peer_field` indica qual campo do registro vira a coluna de contato
        ("sender" para recebidas, "recipient" para enviadas); `fetch` é o método
        do serviço que busca os registros no servidor.

        Se `revealable` (apenas recebidas), o conteúdo das mensagens "não lidas"
        fica OCULTO; clicar no status 'não lida' revela o texto, confirma a
        leitura ao servidor (ACK → grava no banco) e muda o status para 'lida'.
        """
        win = tk.Toplevel(self.root)
        win.title(f"{title} — {self.service.contact_name}")
        win.geometry("760x440")

        ttk.Label(win, padding=8, text=help_text).pack(side=tk.TOP, fill=tk.X)

        # Barra de ferramentas: botão Atualizar + contador de mensagens.
        toolbar = ttk.Frame(win, padding=(8, 0))
        toolbar.pack(side=tk.TOP, fill=tk.X)
        count_lbl = ttk.Label(toolbar, text="")
        count_lbl.pack(side=tk.LEFT)

        columns = ("data", "hora", "peer", "mensagem", "status")
        tree = ttk.Treeview(win, columns=columns, show="headings")
        headings = {
            "data": ("Data", 90),
            "hora": ("Hora", 80),
            "peer": (peer_heading, 110),
            "mensagem": ("Mensagem", 320),
            "status": ("Status", 90),
        }
        for col, (text, width) in headings.items():
            tree.heading(col, text=text)
            tree.column(col, width=width,
                        anchor=tk.W if col in ("mensagem", "peer") else tk.CENTER)
        # Realce visual: não lidas em vermelho, lidas em verde.
        tree.tag_configure("unread", foreground="#B71C1C")
        tree.tag_configure("read", foreground="#1B5E20")

        scroll = ttk.Scrollbar(win, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0), pady=(0, 8))

        #: item da Treeview -> registro (para revelar/confirmar ao clicar)
        row_recs: dict[str, dict] = {}

        def row_values(rec: dict) -> tuple:
            data, hora = _split_ts(rec.get("timestamp", ""))
            is_read = bool(rec.get("read"))
            status = "lida" if is_read else "não lida"
            peer = (rec.get(peer_field) or "").strip().upper()
            # Recebidas não lidas: oculta o conteúdo até o cliente clicar no status.
            body = (rec.get("body", "") if (is_read or not revealable)
                    else self._HIDDEN_BODY)
            return (data, hora, peer, body, status)

        def populate() -> None:
            """(Re)consulta o servidor e repinta a tabela. Usado no load e no Atualizar."""
            try:
                records = fetch()
            except Exception as exc:
                messagebox.showerror(
                    "Histórico",
                    f"Não foi possível consultar o histórico no servidor:\n{exc}",
                    parent=win,
                )
                return
            tree.delete(*tree.get_children())
            row_recs.clear()
            for rec in records:
                is_read = bool(rec.get("read"))
                item = tree.insert(
                    "", tk.END, values=row_values(rec),
                    tags=("read" if is_read else "unread",),
                )
                row_recs[item] = rec
            count_lbl.config(
                text=(f"{len(records)} mensagem(ns)" if records
                      else "Nenhuma mensagem no histórico."))

        def on_click(event) -> None:
            """Clique no status 'não lida': revela o conteúdo e marca como lida."""
            if not revealable or tree.identify_region(event.x, event.y) != "cell":
                return
            if tree.identify_column(event.x) != "#5":  # coluna "status"
                return
            item = tree.identify_row(event.y)
            rec = row_recs.get(item)
            if not rec or bool(rec.get("read")):
                return  # já lida (ou linha vazia)
            mid = rec.get("msg_id")
            if not mid:
                return
            # Confirma a leitura ao servidor: marca 'lida' e remove da fila (DB).
            try:
                self.service.ack_read(mid)
            except Exception as exc:
                messagebox.showerror(
                    "Histórico", f"Falha ao marcar como lida:\n{exc}", parent=win)
                return
            rec["read"] = True
            # Evita que o flush/entrega ao vivo a renderize de novo (já lida aqui).
            self._seen_msg_ids.add(mid)
            tree.item(item, values=row_values(rec), tags=("read",))

        tree.bind("<Button-1>", on_click)

        ttk.Button(toolbar, text="↻ Atualizar", command=populate).pack(side=tk.RIGHT)
        populate()  # carga inicial

    # ------------------------------------------------------------------ #
    # Laços periódicos (ponte de threads + presença)
    # ------------------------------------------------------------------ #
    def _drain_inbox(self) -> None:
        """Consome eventos vindos do ClientCallback (thread do daemon Pyro5)."""
        try:
            while True:
                event = self.service.inbox.get_nowait()
                kind = event[0]
                if kind == "message":
                    _, sender, body, ts, msg_id = event
                    self._receive(
                        Message(sender, self.service.contact_name, body, ts, msg_id))
                elif kind == "pending":  # mensagens da fila offline, no login
                    _, sender, body, ts, msg_id = event
                    self._receive(
                        Message(sender, self.service.contact_name, body, ts, msg_id),
                        queued=True)
                elif kind == "rejection":
                    _, rejecter, original_body = event
                    self._show_rejection(rejecter, original_body)
                elif kind == "status":
                    self._poll_statuses()
        except queue.Empty:
            pass
        self.root.after(_POLL_INBOX_MS, self._drain_inbox)

    def _poll_statuses(self) -> None:
        names = self.contacts.all()
        try:
            statuses = self.service.statuses(names) if self.online else {}
        except Exception:
            statuses = {}
        # Sem servidor/offline: assume offline para todos.
        self._statuses = {n: statuses.get(n, Status.OFFLINE.value) for n in names}
        self._refresh_contacts()
        self._update_chat_status()
        self.root.after(_POLL_STATUS_MS, self._poll_statuses)

    # ------------------------------------------------------------------ #
    def _on_close(self) -> None:
        try:
            self.service.shutdown()
        finally:
            self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()
