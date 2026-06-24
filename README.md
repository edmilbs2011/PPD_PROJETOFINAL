# PPD — Sistema de Mensagens com Controle de Mensagens Offline

Chat de contatos com entrega instantânea (online) e filas offline gerenciadas por
um MOM no servidor, acessado via RMI (Pyro5). UI em Tkinter.

## Funcionalidades

- **Lista de contatos** sempre visível, com presença ● online / ○ offline.
- **Entrega instantânea** quando o destinatário está online; **fila offline** (persistente
  em SQLite) quando não está.
- **Confirmação de leitura (ACK):** a mensagem só é marcada como *lida* e removida da fila
  quando o cliente **exibe** o conteúdo — entrega *at-least-once* (nada se perde entre
  desenfileirar e exibir; reentregas são deduplicadas por `msg_id`).
- **Histórico de mensagens** consultável no servidor, em duas janelas:
  - **Recebidas** — Data · Hora · Remetente · Mensagem · Status. Mensagens *não lidas*
    aparecem com o conteúdo **oculto**; clicar no status "não lida" revela o texto e o
    marca como *lida* (grava no banco).
  - **Enviadas** — Data · Hora · Destinatário · Mensagem · Status (aqui *lida* = o
    destinatário já recebeu/leu).
  - Botão **↻ Atualizar** em ambas.
- **Conversa persistente:** ao reabrir o cliente, o painel de conversa é reconstruído a
  partir do servidor (fonte de verdade).
- **Mensagens de não-contato:** ao receber de alguém fora da lista, o cliente pode aceitar
  (inclui o contato) ou recusar (descarta e avisa o remetente).

## Instalação

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

## Execução (3 passos, em terminais separados)

```bash
# 1) Pyro5 Name Server
./run_nameserver.sh            # ou: python -m Pyro5.nameserver

# 2) Servidor de Mensagens
./run_server.sh                # ou: python -m server.main

# 3) Um cliente por usuário
./run_client.sh alice          # ou: python -m client.main alice

./run_client.sh bob
```

## Como testar o fluxo offline

1. Suba o name server e o servidor.
2. Abra o cliente `alice` (fica online automaticamente ao entrar).
3. No `alice`, adicione o contato `bob` (+ Adicionar) e envie uma mensagem.
   - Como `bob` está offline, a mensagem vai para a **fila** (aparece `offline → fila`)
     e o servidor loga `QUEUED`.
4. Abra o cliente `bob`. Ao entrar, ele recebe a mensagem acumulada (flush da fila) e,
   ao exibi-la, confirma a leitura (ACK) ao servidor.
5. Com ambos online, mensagens são entregues instantaneamente (log `DELIVERED`).
6. Marque/desmarque o checkbox **Online** para alternar de estado (Requisito 2).
7. Em **Histórico → Recebidas/Enviadas** confira data/hora, contato, conteúdo e o status
   (lida/não lida). No `bob`, uma mensagem ainda *não lida* aparece oculta — clique no
   status "não lida" para revelá-la (passa a *lida* no banco).

## Execução em máquinas diferentes

```bash
# Na máquina do name server / servidor:
python -m Pyro5.nameserver --host 0.0.0.0

# Nos clientes (apontando para o host do name server):
export PPD_NS_HOST=<ip-do-servidor>
export PPD_HOST=<ip-local-do-cliente>
python -m client.main alice
```

## Estrutura

```
common/        models.py, config.py
server/        main.py, message_server.py, presence.py, mom.py
client/        main.py, ui.py, service.py, callback.py, contacts.py
server_data/   mom.db        (filas + histórico; criado no 1º start)
client_data/   <nome>_contacts.json  (lista de contatos por cliente)
```
