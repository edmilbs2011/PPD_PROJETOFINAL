"""Bootstrap do cliente.

Uso:
    python -m client.main <nome_de_contato>

Se o nome não for passado (ou for inválido/duplicado), é solicitado em diálogo.
O nome é **case-insensitive** e não pode coincidir com o de outro cliente
conectado.

Pré-requisitos: Name Server e Servidor de Mensagens já em execução.
"""
from __future__ import annotations

import sys
import tkinter as tk
from tkinter import messagebox, simpledialog

from common import config
from common.models import validate_contact_name
from client.contacts import ContactBook
from client.service import ClientService
from client.ui import ChatWindow


def _choose_name(root: tk.Tk, initial: str | None) -> str | None:
    """Obtém um nome válido e disponível, repetindo em caso de erro.

    Retorna o nome normalizado, ou None se o usuário cancelar.
    """
    candidate = initial
    while True:
        if candidate is None:
            candidate = simpledialog.askstring(
                "Entrar", "Seu nome de contato:", parent=root
            )
            if candidate is None:  # usuário cancelou
                return None

        # 1) Critica o formato do nome e o normaliza para MAIÚSCULAS,
        #    eliminando qualquer distinção de caixa daí em diante.
        try:
            name = validate_contact_name(candidate).upper()
        except ValueError as exc:
            messagebox.showerror("Nome inválido", str(exc), parent=root)
            candidate = None
            continue

        # 2) Critica a unicidade (case-insensitive) no servidor.
        try:
            available = ClientService.is_name_available(name)
        except Exception as exc:
            messagebox.showerror(
                "Servidor",
                f"Não foi possível contatar o servidor:\n{exc}",
                parent=root,
            )
            return None

        if not available:
            messagebox.showwarning(
                "Nome em uso",
                f"Já existe um cliente conectado com o nome '{name}'.\n"
                "Escolha outro nome.",
                parent=root,
            )
            candidate = None
            continue

        return name


def main() -> None:
    config.ensure_data_dirs()

    # Raiz temporária só para os diálogos de escolha de nome.
    chooser_root = tk.Tk()
    chooser_root.withdraw()
    contact_name = _choose_name(
        chooser_root, sys.argv[1] if len(sys.argv) > 1 else None
    )
    chooser_root.destroy()
    if not contact_name:
        print("Entrada cancelada.")
        return

    service = ClientService(contact_name)
    service.start_daemon()

    contacts = ContactBook(config.contacts_path(contact_name), owner=contact_name)

    # Requisito 7: ao entrar, solicita a criação da fila e recebe pendentes.
    window = ChatWindow(service, contacts)
    try:
        pending = service.register()
        window.online = True
        window.online_var.set(True)
        window.conn_lbl.config(text="● online", foreground="green")
        # Reconstrói o painel de conversa a partir do servidor (enviadas + recebidas
        # já lidas), para a conversa reabrir preenchida após reiniciar o cliente.
        window.load_history()
        # Encaminha pendentes (não lidas) pela inbox: processadas após o mainloop
        # iniciar, para que diálogos (aceitar/rejeitar não-contato) tenham UI e o
        # ACK seja enviado só depois de exibidas.
        for m in pending:
            service.inbox.put(("pending", m.sender, m.body, m.timestamp, m.msg_id))
    except ValueError as exc:
        # Corrida: o nome foi tomado entre a checagem e o registro.
        messagebox.showerror("Nome em uso", str(exc))
        service.shutdown()
        window.root.destroy()
        return
    except Exception as exc:
        window.conn_lbl.config(text="● sem servidor", foreground="red")
        print(f"Aviso: não foi possível registrar no servidor: {exc}")

    window.run()


if __name__ == "__main__":
    main()
