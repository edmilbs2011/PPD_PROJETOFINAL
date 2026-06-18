"""Bootstrap do Servidor de Mensagens.

Sobe o daemon Pyro5, instancia MOM + PresenceRegistry + MessageServer e
registra o objeto no Name Server sob o nome lógico `PPD.messageserver`.

Pré-requisito: o Pyro5 Name Server deve estar rodando
    python -m Pyro5.nameserver
"""
from __future__ import annotations

import Pyro5.api

from common import config
from server.message_server import MessageServer
from server.mom import MessageQueueManager
from server.presence import PresenceRegistry


def main() -> None:
    config.ensure_data_dirs()

    mom = MessageQueueManager(config.MOM_DB_PATH)
    presence = PresenceRegistry()
    server = MessageServer(mom, presence)

    daemon = Pyro5.api.Daemon(host=config.PYRO_HOST)
    uri = daemon.register(server)

    ns = Pyro5.api.locate_ns(host=config.NS_HOST, port=config.NS_PORT)
    ns.register(config.SERVER_NAME, uri)

    print(f"Servidor de Mensagens pronto como '{config.SERVER_NAME}'")
    print(f"  URI: {uri}")
    print("Aguardando requisições (Ctrl+C para encerrar)...")
    try:
        daemon.requestLoop()
    except KeyboardInterrupt:
        print("\nEncerrando servidor...")
    finally:
        try:
            ns.remove(config.SERVER_NAME)
        except Exception:
            pass
        mom.close()
        daemon.close()


if __name__ == "__main__":
    main()
