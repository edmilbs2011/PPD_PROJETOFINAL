"""Bootstrap do Servidor de Mensagens.

Sobe o daemon Pyro5 e monta a cadeia de colaboradores do Broker:
MOM (durabilidade) + PresenceRegistry (presença) + SubscriptionRegistry
(assinaturas) → MessageBroker (publish/subscribe) → MessageServer (fachada RMI).
Registra o objeto no Name Server sob o nome lógico `PPD.messageserver`.

Pré-requisito: o Pyro5 Name Server deve estar rodando
    python -m Pyro5.nameserver
"""
from __future__ import annotations

import Pyro5.api

from common import config
from server.broker import MessageBroker
from server.message_server import MessageServer
from server.mom import MessageQueueManager
from server.presence import PresenceRegistry
from server.subscriptions import SubscriptionRegistry


def main() -> None:
    config.ensure_data_dirs()

    mom = MessageQueueManager(config.MOM_DB_PATH)
    presence = PresenceRegistry()
    subscriptions = SubscriptionRegistry()
    broker = MessageBroker(mom, presence, subscriptions)
    server = MessageServer(broker, mom, presence)

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
