# Arquitetura — Sistema de Mensagens com Controle de Mensagens Offline

> Projeto Final de PPD (Programação Paralela e Distribuída)
> Python 3.14 · Pyro5 (RMI) · MOM (filas por cliente) · Tkinter (UI)

---

## 1. Visão geral

O sistema é uma aplicação de **troca de mensagens** estilo "chat de contatos" onde:

- Cada cliente tem um **nome de contato** único, usado para se identificar e para registrar amigos.
- Cada cliente mantém uma **lista de contatos** (amigos) sempre visível na UI.
- A entrega é **instantânea quando o destinatário está online** e **armazenada em fila quando está offline**.
- O armazenamento offline é responsabilidade de um **Servidor de Mensagens** remoto, acessado por **RMI via Pyro5**.
- O Servidor mantém, para **cada cliente**, uma **fila gerenciada por um Middleware Orientado a Mensagens (MOM)**.

O padrão arquitetural é **cliente-servidor com callbacks remotos** (publish/deliver):
o Servidor atua como *diretório de presença* + *broker de mensagens offline (MOM)*, e cada
cliente expõe um *objeto remoto de callback* para receber mensagens em tempo real quando online.

```
                         Pyro5 Name Server (pyro5-ns)
                                    │  (resolve nomes lógicos -> URIs)
            ┌───────────────────────┴────────────────────────┐
            │                                                 │
   ┌────────▼─────────┐                              ┌────────▼─────────┐
   │   CLIENTE A      │                              │   CLIENTE B      │
   │  (Tkinter UI)    │                              │  (Tkinter UI)    │
   │                  │      RMI (Pyro5 proxy)       │                  │
   │  ClientCallback  │◄────────────┐   ┌───────────►│  ClientCallback  │
   │  (objeto remoto) │             │   │            │  (objeto remoto) │
   └────────┬─────────┘             │   │            └─────────┬────────┘
            │ RMI                   │   │ entrega              │ RMI
            │ (register/send/...)   │   │ instantânea          │
            ▼                       │   │ (callback)           ▼
   ┌────────────────────────────────┴───┴──────────────────────────────┐
   │                     SERVIDOR DE MENSAGENS (Pyro5)                   │
   │                                                                    │
   │  ┌────────────────┐   ┌──────────────────┐   ┌──────────────────┐  │
   │  │ PresenceRegistry│   │  Roteador/Dispatch│   │       MOM        │  │
   │  │ nome -> estado, │   │  online? -> push  │   │  fila por cliente │  │
   │  │ nome -> URI     │   │  offline? -> fila │   │  (persistente)    │  │
   │  └────────────────┘   └──────────────────┘   └──────────────────┘  │
   └────────────────────────────────────────────────────────────────────┘
```

---

## 2. Componentes

### 2.1 Pyro5 Name Server
Processo padrão do Pyro5 (`python -m Pyro5.nameserver`). Permite que clientes localizem o
Servidor de Mensagens por um **nome lógico** (`PPD.messageserver`) em vez de URI fixa, e que o
Servidor localize os callbacks dos clientes online por `PPD.client.<nome>`.

### 2.2 Servidor de Mensagens (`server/`)
Objeto remoto Pyro5 (`@Pyro5.expose`) registrado como `PPD.messageserver`. Responsabilidades:

1. **PresenceRegistry** — mantém o estado de cada cliente:
   - `nome_contato` (identidade única)
   - `status` ∈ {ONLINE, OFFLINE}
   - `callback_uri` (URI Pyro do objeto de callback, válida só quando ONLINE)
2. **MOM (Message-Oriented Middleware)** — uma **fila FIFO por cliente**, persistente em disco.
   Garante que mensagens enviadas a um destinatário offline não se percam.
3. **Roteador / Dispatcher** — decide entre **entrega instantânea** (callback remoto) e
   **enfileiramento** (MOM), conforme o estado do destinatário.

> O Servidor **não** valida amizade no envio por padrão (a lista de contatos é mantida no
> cliente). É possível, opcionalmente, espelhar contatos no servidor — ver §8.

### 2.3 Cliente (`client/`)
Cada cliente é, ao mesmo tempo:
- **Consumidor de RMI**: usa um *proxy* Pyro5 para chamar o Servidor (registrar, enviar, mudar
  status, gerenciar contatos).
- **Provedor de RMI**: executa um *daemon* Pyro5 e expõe um `ClientCallback` (objeto remoto)
  para que o Servidor **empurre** mensagens em tempo real quando o cliente está online.
- **Aplicação Tkinter**: UI com lista de contatos sempre visível, janela de conversa,
  botão de online/offline e gestão de contatos.

### 2.4 MOM — Middleware Orientado a Mensagens (`server/mom.py`)
Implementação própria do middleware de filas, com **dois armazenamentos separados** em SQLite:
- **Fila** (tabela `messages`) — `dict[nome_contato -> Deque[Message]]` persistido. Guarda
  **apenas as mensagens pendentes** (encaminhadas àquela fila/seção do servidor e ainda não
  entregues). Uma mensagem sai daqui assim que é entregue ao destinatário.
- **Histórico permanente** (tabela `message_log`, com coluna `read_at`) — armazena **todas** as
  mensagens que passam pelo servidor e **nunca** é esvaziado. É a fonte da *consulta de histórico
  do cliente*.
- **Status de leitura por ACK explícito**: toda mensagem nasce "não lida" (`read_at = NULL`). O
  cliente confirma com `ack_read(msg_id)` **somente depois de renderizá-la na UI**; o servidor
  então grava `read_at` ("lida") e remove a mensagem da fila. Entrega **at-least-once com
  confirmação**: a mensagem só sai da fila quando há prova de recebimento — uma queda do cliente
  entre desenfileirar e exibir **não a perde** (continua na fila e é reentregue no próximo login).
  Reentregas são deduplicadas no cliente por `msg_id`, e `ack_read` é idempotente.
- **Persistência** (SQLite) para sobreviver a reinício do servidor — característica central de
  "mensagens offline".
- Operações: `create_queue`, `enqueue`, `peek` (flush não-destrutivo), `ack_read`, `reject`,
  `delete_queue`, `log_message`, `history_for`.
- Acesso protegido por **lock** (o daemon Pyro5 atende requisições concorrentes).

---

## 3. Interfaces remotas (contratos RMI / Pyro5)

### 3.1 `MessageServer` (exposto pelo Servidor)
```python
class MessageServer:
    # Requisito 7: ao entrar, o cliente pede a criação de sua fila
    def register(self, contact_name: str, callback_uri: str) -> dict
        # cria a fila no MOM (se ainda não existe), marca ONLINE,
        # guarda a URI de callback e devolve as mensagens pendentes da fila.

    def unregister(self, contact_name: str) -> None
        # cliente saindo do sistema; libera presença.

    # Requisito 2: mudar de estado on/off
    def set_status(self, contact_name: str, online: bool, callback_uri: str | None) -> list
        # ONLINE  -> registra URI e faz flush (peek) devolvendo as pendentes;
        #            elas só saem da fila quando o cliente confirma com ack_read
        # OFFLINE -> remove URI; mensagens futuras vão para a fila

    # Requisitos 3, 4, 6: envio com decisão online/offline
    def send_message(self, sender: str, recipient: str, body: str) -> str
        # retorna "DELIVERED" (push instantâneo) ou "QUEUED" (foi para a fila do destinatário)

    # Consulta de presença para a UI (status dos contatos)
    def get_status(self, contact_name: str) -> str           # "ONLINE"|"OFFLINE"|"UNKNOWN"
    def get_statuses(self, names: list[str]) -> dict          # nome -> status

    def fetch_offline(self, contact_name: str) -> list        # puxa fila manualmente (fallback/polling)

    # Consulta de histórico de mensagens do cliente (só RECEBIDAS — Requisito 1)
    def get_history(self, contact_name: str) -> list          # [{sender, recipient, body, timestamp, read, read_at}]
        # histórico das mensagens recebidas; read=True ("lida") se houve ACK do
        # cliente, read=False ("não lida") se ainda pendente (nunca confirmada)

    # Consulta de histórico de mensagens ENVIADAS pelo cliente
    def get_sent(self, contact_name: str) -> list             # [{sender, recipient, body, timestamp, read, read_at}]
        # mensagens enviadas; aqui read=True ("lida") quando o DESTINATÁRIO
        # confirmou (ACK), read=False ("não lida") enquanto pendente na fila dele

    # Conversa completa (ENVIADAS + RECEBIDAS) p/ reconstruir o painel no startup
    def get_conversation(self, contact_name: str) -> list     # [{msg_id, peer, direction, sender, body, timestamp, read}]
        # fonte de verdade da conversa: ao reabrir o cliente, o painel é
        # remontado a partir do servidor (não fica mais vazio entre sessões)

    # ACK de leitura: confirma recebimento+exibição, marca "lida" e remove da fila
    def ack_read(self, contact_name: str, msg_id: str) -> bool   # idempotente
```

> **Entrega confiável (at-least-once):** o servidor **não** apaga a mensagem da fila ao
> entregá-la. Ela permanece até o cliente chamar `ack_read(msg_id)` — o que só ocorre **depois**
> de a mensagem ser renderizada na UI. Assim, uma falha entre desenfileirar e exibir não perde a
> mensagem (reentrega no próximo login). O cliente deduplica por `msg_id`; `ack_read` é idempotente.

### 3.2 `ClientCallback` (exposto por cada Cliente)
```python
class ClientCallback:
    # Requisito 3: entrega instantânea quando o destinatário está online.
    # msg_id acompanha a mensagem para que o cliente envie o ack_read após exibi-la.
    def receive_message(self, sender: str, body: str, timestamp: str, msg_id: str) -> None
    def notify_status(self, contact_name: str, status: str) -> None   # (opcional) presença em tempo real
    def ping(self) -> bool                                            # health-check do servidor
```

### 3.3 Modelo de mensagem
```python
@dataclass
class Message:
    sender: str
    recipient: str
    body: str
    timestamp: str          # ISO-8601
    msg_id: str             # uuid4
```

---

## 4. Fluxos principais (diagramas de sequência)

### 4.1 Entrada no sistema (Requisito 7)
```
Cliente                         Servidor (MOM + Presence)
  │  inicia daemon Pyro5, cria ClientCallback (URI)
  │  register("alice", uri_alice)
  ├──────────────────────────────────►│
  │                                    │ MOM.create_queue("alice")  (se não existir)
  │                                    │ Presence["alice"] = ONLINE, uri
  │                                    │ pendentes = MOM.peek("alice")  (NÃO remove da fila)
  │◄──────────────────────────────────┤ return pendentes
  │  renderiza pendentes na UI
  │  para cada msg: ack_read(alice, msg_id)  ──►│ read_at=agora; remove da fila
```

### 4.2 Envio com destinatário ONLINE (Requisitos 3)
```
Alice                 Servidor                         Bob (callback)
  │ send_message(alice, bob, "oi")
  ├────────────────────►│
  │                     │ MOM.log_message(...)            # histórico, "não lida"
  │                     │ MOM.enqueue("bob", ...)         # fica na fila até o ACK
  │                     │ Presence[bob] == ONLINE
  │                     │ proxy.receive_message("alice","oi",ts,msg_id)
  │                     ├─────────────────────────────────►│ exibe na UI
  │◄────────────────────┤ return "DELIVERED"               │ ack_read(bob,msg_id)
  │                     │◄─────────────────────────────────┤ (após exibir)
  │                     │ read_at=agora; remove da fila
```

### 4.3 Envio com destinatário OFFLINE (Requisitos 4, 5, 6)
```
Alice                 Servidor (MOM)
  │ send_message(alice, bob, "oi")
  ├────────────────────►│
  │                     │ MOM.log_message(...)               # histórico, "não lida"
  │                     │ Presence[bob] == OFFLINE
  │                     │ MOM.enqueue("bob", Message(...))   # fila do destinatário
  │                     │ persiste em disco
  │◄────────────────────┤ return "QUEUED"
```

### 4.4 Bob volta a ficar ONLINE (flush da fila — Requisitos 2, 4)
```
Bob                    Servidor (MOM)
  │ set_status(bob, online=True, uri_bob)
  ├────────────────────►│
  │                     │ Presence[bob] = ONLINE, uri
  │                     │ pendentes = MOM.peek("bob")   # NÃO esvazia (espera ACK)
  │◄────────────────────┤ return pendentes
  │ exibe mensagens acumuladas na UI
  │ para cada msg: ack_read(bob, msg_id)  ──►│ read_at=agora; remove da fila
  │ (queda antes do ACK → msg permanece e é reentregue no próximo login)
```

---

## 5. Modelo de concorrência e threads

O ponto mais delicado da implementação. **Tkinter exige que toda atualização de UI ocorra na
thread do `mainloop`**, enquanto o **daemon Pyro5 atende callbacks em outra(s) thread(s)**.

Estratégia no cliente:
- **Thread principal**: `tk.mainloop()` (UI).
- **Thread secundária**: `pyro_daemon.requestLoop()` (recebe `receive_message` do servidor).
- **Ponte thread-safe**: o callback NÃO toca widgets diretamente. Ele coloca a mensagem em uma
  `queue.Queue` (inbox). A UI consome essa fila via `root.after(100, drain_inbox)` (polling
  leve no laço do Tk), atualizando os widgets na thread correta.

No servidor:
- O daemon Pyro5 pode atender requisições concorrentes → **MOM e PresenceRegistry usam
  `threading.Lock`** (ou `RLock`) para consistência.
- Cuidado para **não chamar o callback do destinatário segurando o lock** (evita deadlock e
  bloqueio prolongado): copiar a URI sob lock, soltar, depois invocar o callback.

```
CLIENTE (processo)
 ├── Thread-main: Tkinter mainloop  ──after()──►  drain inbox ──► widgets
 └── Thread-pyro: daemon.requestLoop ──► ClientCallback.receive_message ──► inbox.put()
```

---

## 6. Estrutura de diretórios

```
PPD_ProjetoFinal/
├── ARQUITETURA.md            # este documento
├── README.md                 # como rodar
├── requirements.txt          # Pyro5 (Tkinter já vem no Python)
├── run_nameserver.sh         # sobe o Pyro5 Name Server
├── run_server.sh             # sobe o Servidor de Mensagens
├── run_client.sh             # sobe um cliente (nome via arg/UI)
│
├── common/
│   ├── __init__.py
│   ├── models.py             # dataclass Message, enums de status
│   └── config.py             # nomes lógicos Pyro, host/porta, caminhos
│
├── server/
│   ├── __init__.py
│   ├── main.py               # bootstrap: daemon + registro no name server
│   ├── message_server.py     # classe MessageServer (@expose) — interface §3.1
│   ├── presence.py           # PresenceRegistry (estado + URIs + lock)
│   └── mom.py                # MOM: filas por cliente + persistência (SQLite)
│
└── client/
    ├── __init__.py
    ├── main.py               # bootstrap do cliente (daemon callback + UI)
    ├── callback.py           # ClientCallback (@expose) — interface §3.2
    ├── service.py            # ClientService: proxy p/ servidor + ponte de threads
    ├── contacts.py           # ContactBook: CRUD da lista de contatos (Requisito 8) + persistência local
    └── ui.py                 # ChatWindow (Tkinter): lista de contatos, chat, on/off
```

---

## 7. Persistência

| Dado | Onde | Mecanismo | Motivo |
|------|------|-----------|--------|
| Filas offline (MOM) | Servidor | SQLite (`server_data/mom.db`, tabela `messages`) | Não perder mensagens em reinício — é o coração do "offline"; guarda só pendentes |
| Histórico de mensagens | Servidor | SQLite (`server_data/mom.db`, tabela `message_log`) | Banco armazena **todas** as mensagens; base da consulta de histórico + status lida/não lida |
| Painel de conversa | Servidor (fonte) → cliente (memória) | Remontado no startup via `get_conversation` | Conversa reabre preenchida entre sessões; servidor é a fonte de verdade (dedup por `msg_id`) |
| Estado de presença | Servidor | Memória (volátil) | Presença é por sessão; ao reiniciar todos voltam OFFLINE |
| Lista de contatos | Cliente | JSON (`client_data/<nome>_contacts.json`) | Requisito 1 e 8; cada cliente é dono da própria lista |

> **Por que SQLite no MOM:** transacional, ACID, embutido no Python (`sqlite3`), suporta
> recuperação após crash. Alternativa mais simples: arquivo JSON por fila com escrita atômica.
> Para um broker "de verdade" poderia-se usar RabbitMQ, mas o enunciado pede um MOM **no
> servidor de mensagens**, então implementamos o middleware como módulo do próprio servidor.

---

## 8. Lista de contatos (Requisitos 1 e 8)

- A lista de amigos é **propriedade do cliente** e persiste localmente (`ContactBook`).
- A UI mostra a lista **o tempo todo** (painel lateral), com indicador de status
  (● online / ○ offline) atualizado por *polling* periódico em `get_statuses(...)` ou por
  callback `notify_status`.
- **Inclusão**: adiciona o nome ao `ContactBook`, persiste e re-renderiza a lista.
- **Exclusão**: remove o nome, persiste e re-renderiza.
- O **nome de contato é a chave de roteamento** usada em `send_message(sender, recipient, ...)`.

Decisão de design: a amizade **não** é obrigatoriamente bidirecional nem validada pelo servidor
(modelo simples, centrado no cliente). Caso o enunciado exija validação, basta o servidor manter
`Set[contato]` por usuário e rejeitar envios entre não-amigos — ponto de extensão isolado no
`MessageServer.send_message`.

---

## 9. Tratamento de falhas

- **Destinatário "online" mas inacessível** (callback falha por timeout/erro): o servidor
  captura a exceção Pyro, marca o destinatário como OFFLINE e **enfileira** a mensagem no MOM
  (degradação graciosa → vira caso offline). Retorna `"QUEUED"`.
- **Servidor indisponível** no cliente: a UI exibe erro e desabilita envio; tenta reconectar.
- **Name Server ausente**: instruções de bootstrap exigem subir o NS primeiro (ver README).
- **Concorrência no MOM**: `Lock` por operação; persistência transacional evita corrupção.
- **Nome de contato duplicado** no `register`: se já estiver ONLINE, o servidor rejeita
  (`NameInUse`) ou assume reconexão (substitui a URI) — escolha documentada no código.

---

## 10. Dependências e execução

**requirements.txt**
```
Pyro5>=5.15
```
(`tkinter`, `sqlite3`, `queue`, `threading`, `dataclasses` são da biblioteca padrão.)

**Ordem de inicialização**
```bash
# 1) Name Server do Pyro5
python -m Pyro5.nameserver            # (ou ./run_nameserver.sh)

# 2) Servidor de Mensagens
python -m server.main                 # (ou ./run_server.sh)

# 3) Um cliente por usuário (em terminais/máquinas distintas)
python -m client.main alice          # (ou ./run_client.sh alice)
python -m client.main bob
```

Para rodar em **máquinas diferentes**, definir `PYRO_HOST`/host do NS em `common/config.py`
e subir o name server com `--host 0.0.0.0`.

---

## 11. Mapa Requisito → Componente

| # | Requisito | Onde é atendido |
|---|-----------|-----------------|
| 1 | Nome de contato + lista sempre visível | `client/contacts.py`, painel em `client/ui.py` |
| 2 | Alternar online/offline | `MessageServer.set_status`, botão na `ui.py` |
| 3 | Entrega instantânea quando online | Dispatcher → `ClientCallback.receive_message` |
| 4 | Offline via servidor remoto RMI (Pyro5) | `MessageServer` + `server/mom.py` |
| 5 | Fila por cliente gerenciada por MOM | `server/mom.py` (uma fila por nome) |
| 6 | Offline → fila do destinatário | `MessageServer.send_message` (ramo OFFLINE) |
| 7 | Ao entrar, pedir criação da fila | `MessageServer.register` → `MOM.create_queue` |
| 8 | Incluir/excluir contatos | `client/contacts.py` (CRUD) + `ui.py` |
| 9 | Banco armazena **todas** as mensagens; fila guarda só pendentes | `MOM.log_message` (tabela `message_log`) + `MOM.enqueue` (tabela `messages`) |
| 10 | Consulta de histórico (Recebidas/Enviadas): data, hora, contato, conteúdo, status | `MessageServer.get_history`/`get_sent` → janelas em `ui.py` |
| 11 | Status lida/não lida com confirmação de leitura (ACK) | `MessageServer.ack_read` → `MOM.ack_read` (ver §12) |
| 12 | Recebida não lida com conteúdo oculto; revelar ao clicar marca lida | `ui.py` `_open_history_window` (`revealable`) → `ack_read` |

> Os requisitos 9–12 foram acrescentados além do enunciado original; o detalhamento
> está na §12.

---

## 12. Histórico de mensagens e confirmação de leitura (ACK)

### 12.1 Dois armazenamentos, papéis distintos
O MOM mantém **duas tabelas** em `server_data/mom.db`, deliberadamente separadas:

| Tabela | Papel | Conteúdo |
|--------|-------|----------|
| `messages` | **Fila** (pendências) | Só mensagens **ainda não confirmadas**. Uma linha sai daqui quando o destinatário envia o ACK. |
| `message_log` | **Histórico permanente** | **Todas** as mensagens roteadas, com a coluna `read_at`. Nunca é esvaziado. |

Invariante mantido pelo sistema: **`não lida` ⟺ a mensagem ainda está na fila**.

```sql
messages(seq, msg_id, queue_name, sender, recipient, body, timestamp)
message_log(seq, msg_id, sender, recipient, body, timestamp, read_at)
```

### 12.2 Confirmação de leitura (ACK) — entrega at-least-once
O status de leitura é uma **confirmação explícita do cliente**, não uma dedução:

1. No envio, `MOM.log_message` grava a mensagem com `read_at = NULL` (**não lida**) e ela é
   enfileirada — inclusive na entrega instantânea.
2. O servidor **não remove** a mensagem da fila ao entregá-la; o flush é `MOM.peek`
   (não-destrutivo).
3. O cliente só chama `ack_read(msg_id)` **depois de renderizar** a mensagem na UI
   (`ChatWindow._receive` → `_ack`).
4. `MOM.ack_read` então, atomicamente: `UPDATE message_log SET read_at = agora`
   (**lida**) **e** `DELETE FROM messages` (sai da fila).

Consequências:
- **Nada se perde** entre desenfileirar e exibir: se o cliente cair antes do ACK, a
  mensagem permanece na fila e é **reentregue** no próximo login (*at-least-once*).
- Reentregas são **deduplicadas no cliente** por `msg_id` (`_seen_msg_ids`); `ack_read` é
  **idempotente** (o `UPDATE ... WHERE read_at IS NULL` só marca na primeira vez).
- **Rejeição** de não-contato usa `MOM.reject`: remove da fila **sem** marcar `read_at`
  (continua "não lida" no histórico, mas não é reentregue).

### 12.3 Consulta de histórico (UI)
Duas janelas, a partir do mesmo construtor (`ChatWindow._open_history_window`):

| Janela | Fonte (servidor) | Coluna de contato | Significado de "lida" |
|--------|------------------|-------------------|-----------------------|
| **Recebidas** | `get_history` | Remetente | **você** já recebeu/exibiu (ACK) |
| **Enviadas**  | `get_sent`    | Destinatário | o **destinatário** já confirmou (ACK) |

Colunas: **Data · Hora · Remetente/Destinatário · Mensagem · Status**, com botão
**↻ Atualizar** (reconsulta o servidor).

Nas **Recebidas**, o conteúdo das mensagens "não lida" fica **oculto**; clicar no status
"não lida" chama `ack_read` (revela o texto, grava `read_at` no banco e remove da fila).
Esse é um segundo caminho de leitura, equivalente ao do chat ao vivo.

### 12.4 Conversa persistente entre sessões
No startup, `ChatWindow.load_history` reconstrói o painel de conversa a partir de
`get_conversation` (enviadas + recebidas **já lidas**) — o servidor é a fonte de verdade,
então a conversa reabre preenchida e sem duplicatas (dedup por `msg_id`). As recebidas
ainda pendentes não são carregadas aqui: chegam pelo flush e passam por `_receive`
(que trata não-contato e dispara o ACK).
|   | UI em Tkinter | `client/ui.py` |
