# finbot

Bot de Telegram pessoal para controle de finanças. Você manda mensagens como
**"gastei 35 no mercado"** e ele registra, categoriza, resume o mês, avisa quando
um orçamento está perto de estourar e exporta tudo para planilha.

Também acompanha seus **cartões de crédito** (limite, faturas montadas pelas
compras que você registra, parcelamentos, estornos, rotativo e o melhor cartão
para usar hoje) e suas **dívidas**: saldo, histórico, data estimada de quitação,
juros que ainda vão ser pagos, simulações, estratégias de quitação, indicadores
de saúde financeira e lembretes de vencimento.

Roda no seu computador, guarda os dados num arquivo SQLite local e só responde
ao seu chat.

## Sumário

- [O que ele faz](#o-que-ele-faz)
- [Instalação](#instalação)
- [Configuração](#configuração)
- [Executar](#executar)
- [Ligar sozinho no Windows](#ligar-sozinho-no-windows-sem-janela)
- [Hospedar num servidor](#hospedar-num-servidor-24-horas-grátis)
- [Como usar](#como-usar)
- [Cartões de crédito](#cartões-de-crédito)
- [Dívidas](#dívidas)
- [Seus dados](#seus-dados)
- [Problemas comuns](#problemas-comuns)
- [Desenvolvimento](#desenvolvimento)
- [Decisões de projeto](#decisões-de-projeto)

## O que ele faz

| Mensagem ou comando | Resultado |
| --- | --- |
| `gastei 35 no mercado` | Registra um gasto de R$ 35,00 em *alimentação* |
| `recebi 3000 salário` | Registra uma entrada de R$ 3.000,00 em *salário* |
| `/hoje` | Lista os registros do dia e os totais |
| `/mes` | Resumo do mês: entradas, gastos, saldo, categorias e orçamentos |
| `/orcamento alimentação 800` | Define um limite mensal; avisa ao passar de 80% e de 100% |
| `/exportar` | Envia um CSV com tudo, pronto para o Excel / Google Planilhas |
| `/desfazer` | Apaga o último registro |
| `/ajuda` ou `/help` | Mostra as instruções |
| `/novocartao` | Cadastra um cartão: fechamento, vencimento, limite, usado, faturas |
| `gastei 80 no mercado no nubank` | Compra no cartão: entra na fatura certa e ocupa o limite |
| `comprei tv 3000 em 10x no nubank` · `/parcelado` | Compra parcelada, uma parcela por fatura |
| `paguei 1500 da fatura do nubank` | Paga a fatura, libera o limite; alerta de rotativo se faltar |
| `/cartoes` · `/cartao nome` | Todos os cartões e o melhor para usar hoje · detalhes de um |
| `/fatura nome [valor]` | Mostra a fatura, ou informa o valor real do banco |
| `/estorno` | Estorno total ou parcial de uma compra |
| `/novadivida` | Cadastra uma dívida, uma pergunta por vez |
| `paguei 450 do empréstimo nubank` | Abate o saldo da dívida e registra o gasto em *dívidas* |
| `/dividas` · `/divida nome` | Resumo de todas · detalhes e histórico de uma |
| `/editardivida nome` | Muda taxa, parcela, vencimento e outros dados |
| `/saldo nome valor` | Corrige o saldo pelo valor do app do banco |
| `/removerdivida nome` | Apaga uma dívida (pede confirmação) |
| `/simular nome extra` | Quanto tempo e juros um extra mensal economiza |
| `/estrategia [extra]` | Avalanche × bola de neve |
| `/saude` | Comprometimento da renda, sobra e sugestões com seus números |
| `/grafico nome` | Imagem com a evolução do saldo e a projeção |
| `/lembretes` | Próximos vencimentos (os avisos chegam sozinhos) |

## Instalação

Requisitos: **Python 3.11 ou mais novo** e uma conta no Telegram.

No PowerShell, dentro da pasta do projeto:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

> Usamos o caminho completo do Python do ambiente virtual em vez de "ativá-lo",
> porque a ativação (`Activate.ps1`) costuma ser bloqueada pela política de
> execução do Windows. No Linux/macOS, troque por `.venv/bin/python`.

O `-e` instala em modo "editável": mudanças no código valem sem reinstalar
(basta reiniciar o bot). O `[dev]` inclui as ferramentas de teste (pytest, ruff).
Rode o mesmo comando de novo sempre que o `pyproject.toml` ganhar dependências
(o módulo de dívidas acrescentou `matplotlib` e o agendador de lembretes).

## Configuração

### 1. Crie o bot e pegue o token

1. No Telegram, abra uma conversa com **@BotFather** e mande `/newbot`.
2. Escolha um nome (ex.: "Meu Finbot") e um username terminado em `bot`.
3. Ele responde com um **token** no formato `123456789:ABCdef...`.

O token é a senha do bot: quem tiver o token controla o bot. Não publique,
não cole em chats e não faça commit dele. Se vazar, mande `/revoke` ao
@BotFather para gerar outro.

### 2. Descubra seu chat id

Mande qualquer mensagem para **@userinfobot**; ele responde com seu `Id`.

### 3. Crie o arquivo `.env`

```powershell
Copy-Item .env.example .env
notepad .env
```

| Variável | Obrigatória | Padrão | Descrição |
| --- | --- | --- | --- |
| `TELEGRAM_TOKEN` | sim | | Token do @BotFather |
| `ALLOWED_CHAT_ID` | sim | | Seu chat id; qualquer outro chat é ignorado |
| `DATABASE_PATH` | não | `data/finbot.db` | Onde fica o banco SQLite |
| `TIMEZONE` | não | `America/Sao_Paulo` | Fuso usado para "hoje" e "este mês" |
| `FIXED_CATEGORIES` | não | `moradia,saúde,educação` | Categorias que contam como gastos fixos no `/saude` |
| `HEALTH_OK_MAX` | não | `30` | Comprometimento da renda até este % é *saudável* |
| `HEALTH_WARN_MAX` | não | `40` | Até este % é *atenção*; acima, *alerta* |
| `SUGGEST_MIN_GROWTH_PCT` | não | `20` | Sugestão só para categoria que cresceu ao menos este %... |
| `SUGGEST_MIN_GROWTH_BRL` | não | `50` | ...e ao menos estes reais |
| `SUGGEST_SHARE_PCT` | não | `50` | % do aumento que a sugestão simula mandar para a dívida |
| `REMINDER_HOUR` | não | `9` | Hora (0 a 23, no seu fuso) em que os lembretes saem |
| `REMINDER_DAYS_BEFORE` | não | `3` | Quantos dias antes do vencimento avisar |

Valor inválido (ex.: `HEALTH_OK_MAX` maior que `HEALTH_WARN_MAX`) faz o bot
parar ao iniciar com uma mensagem dizendo qual variável corrigir.

O `.env` já está no `.gitignore`.

## Executar

```powershell
.\.venv\Scripts\python.exe -m finbot
```

Rode a partir da pasta do projeto (é de lá que o `.env` é lido). Quando aparecer
`Application started`, o bot está no ar: abra a conversa com ele e mande `/start`.

- O bot só funciona **enquanto o terminal estiver aberto**. `Ctrl+C` para parar.
- Depois de mudar o código, pare e rode de novo (ele não recarrega sozinho).
- Não rode duas cópias ao mesmo tempo (dá erro `Conflict`).

## Ligar sozinho no Windows (sem janela)

O Agendador de Tarefas do Windows pode iniciar o bot em segundo plano sempre que
você entrar no Windows. Sem janela (`pythonw.exe`), o log vai para
`data\finbot.log` (até 3 arquivos de 1 MB). Para criar a tarefa, rode no
PowerShell (não precisa de administrador):

```powershell
$proj = "C:\Users\SEU_USUARIO\projetos\finbot"   # pasta do projeto
$user = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute "$proj\.venv\Scripts\pythonw.exe" -Argument "-m finbot" -WorkingDirectory $proj
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user; $trigger.Delay = "PT30S"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName "finbot" -Action $action -Trigger $trigger -Settings $settings -Principal $principal
```

O que essa tarefa faz: liga 30 s depois de você entrar (tempo da internet subir),
roda sem limite de tempo e também na bateria, tenta de novo a cada minuto se
cair, e nunca abre duas cópias. Sem janela, o bot insiste na conexão inicial até
a internet aparecer.

| Para... | Comando |
| --- | --- |
| Ligar agora | `Start-ScheduledTask finbot` |
| Parar | `Stop-ScheduledTask finbot` |
| Ver se está rodando | `Get-ScheduledTask finbot` (State = Running) |
| Ver o log ao vivo | `Get-Content data\finbot.log -Tail 20 -Wait` |
| Não ligar mais sozinho | `Disable-ScheduledTask finbot` (volta com `Enable-ScheduledTask`) |
| Remover a tarefa | `Unregister-ScheduledTask finbot -Confirm:$false` |

Atenção: **pare a tarefa antes de rodar o bot no terminal** (duas cópias dão
`Conflict`), e depois de atualizar o código use `Stop-ScheduledTask finbot` e
`Start-ScheduledTask finbot`. Com o PC desligado ou em suspensão o bot não
responde; lembretes e rotativo atrasados saem quando ele voltar.

## Hospedar num servidor (24 horas, grátis)

Para o bot ficar ligado sem depender do seu computador, use uma VM com disco
próprio. Serviços que "dormem" quando ninguém acessa ou que apagam os arquivos
a cada reinício **não servem**: o banco `data/finbot.db` seria perdido.

Opção recomendada: a VM **e2-micro** do *free tier* do Google Cloud (grátis
dentro dos limites; pede cartão, crie um alerta de orçamento para garantir).

1. **Criar a VM** (Compute Engine → Criar instância): região `us-central1`,
   `us-west1` ou `us-east1`; tipo **e2-micro**; disco **Debian 12**,
   **standard persistent disk** de até **30 GB** (o "balanceado" é cobrado).
2. **Abrir o terminal** pelo botão **SSH** do console.
3. **Baixar o projeto** (repositório privado no GitHub):
   ```bash
   sudo apt-get update && sudo apt-get install -y git
   git clone https://github.com/SEU_USUARIO/finbot.git && cd finbot
   ```
   Repositório privado pede login: use um *token* de acesso pessoal do GitHub
   como senha (GitHub → Settings → Developer settings → Personal access tokens).
4. **Instalar e configurar:**
   ```bash
   bash deploy/instalar.sh   # na 1ª vez ele cria o .env e para
   nano .env                 # cole TELEGRAM_TOKEN e ALLOWED_CHAT_ID
   bash deploy/instalar.sh   # agora instala e liga o bot
   ```
5. **Pare o bot no seu computador** (duas cópias ao mesmo tempo dão `Conflict`).

O `instalar.sh` deixa o bot como serviço do sistema: liga sozinho quando a VM
reinicia e religa em 10 segundos se cair. Também agenda um **backup diário** do
banco às 03:30 em `data/backups/diario-*.db` (guarda os 14 mais recentes).
O fuso não muda: o bot usa o `TIMEZONE` do `.env`, não o horário do servidor.

| No servidor | Comando |
| --- | --- |
| Ver o log ao vivo | `journalctl -u finbot -f` |
| Reiniciar | `sudo systemctl restart finbot` |
| Atualizar depois de um `git push` | `bash deploy/atualizar.sh` (faz backup, `git pull`, reinstala e reinicia) |
| Backup agora | `bash deploy/backup.sh` |
| Levar seus dados atuais para lá | antes de instalar, envie o `data/finbot.db` pelo menu ⚙️ → *Fazer upload* do SSH e mova para `~/finbot/data/` |
| Trazer um backup para o seu PC | menu ⚙️ → *Fazer download* → `~/finbot/data/backups/...` |

## Como usar

### Registrar gastos e entradas

Qualquer mensagem de texto que não seja comando vira um registro. O formato é
tolerante e a ordem das palavras não importa:

```
gastei 35 no mercado          -> gasto,   R$ 35,00,    alimentação
12,50 uber                    -> gasto,   R$ 12,50,    transporte
R$ 1.234,56 aluguel           -> gasto,   R$ 1.234,56, moradia
recebi 3000 salário           -> entrada, R$ 3.000,00, salário
80 presente #família          -> gasto,   R$ 80,00,    família (forçada)
```

Regras:

- **Valor**: o primeiro número da mensagem. Aceita `35`, `12,50`, `12.5`,
  `1.500` (mil e quinhentos), `1.234,56` e `R$35`. Os demais números ficam
  na descrição (`15 no 99` é R$ 15 de transporte, descrição "99").
- **Tipo**: é **entrada** se houver *recebi, ganhei, entrou, entrada, receita*;
  senão é **gasto**.
- **Categoria**: adivinhada por palavras-chave (*mercado* → alimentação,
  *uber* → transporte, ...). Sem correspondência, vira *outros* (gastos) ou
  *receita* (entradas). Use `#categoria` para escolher você mesmo.
- **Descrição**: o resto da mensagem, sem palavras vazias como "no", "de", "com".

Para ensinar palavras novas, edite os dicionários em
[`src/finbot/parsing/categories.py`](src/finbot/parsing/categories.py)
(escreva as palavras sem acento) e reinicie o bot.

Errou? Mande `/desfazer`: apaga o registro mais recente (pode repetir).

### Consultar

- `/hoje`: registros do dia, com hora, e os totais.
- `/mes`: resumo do mês atual. `/mes 10` para outubro deste ano, `/mes 10/2025`
  para outro ano (também aceita `10-2025` e `10/25`).

"Hoje" e "mês" seguem o `TIMEZONE`: um gasto às 23:30 do dia 31 conta nesse dia,
mesmo que em UTC já seja o dia seguinte.

### Orçamentos

```
/orcamento alimentação 800       define (ou troca) o limite mensal
/orcamento comida fora 300,50    a categoria pode ter várias palavras
/orcamento                       mostra o uso de cada orçamento no mês
/orcamento remover alimentação   apaga o limite
```

- Acentos e maiúsculas não importam: `alimentacao` vale para `alimentação`.
- Ao registrar um gasto, o bot avisa **uma vez** quando a categoria cruza 80%
  e **uma vez** quando cruza 100% do limite no mês.
- O `/mes` mostra os orçamentos com ⚠️ (80% ou mais) e 🚨 (estourado).
- O limite vale para todos os meses; não há histórico de limites antigos.

### Exportar

- `/exportar`: CSV com todos os registros.
- `/exportar 10/2025`: só um mês.

O arquivo abre direto no Excel em português: colunas separadas por `;`,
valores com vírgula (`35,00`), datas `dd/mm/aaaa` no seu fuso e acentos
preservados. Colunas: `data;hora;tipo;valor;categoria;descricao`.

## Cartões de crédito

Cartão tem cadastro próprio, separado das dívidas: ele só vira dívida de verdade
quando a fatura não é paga inteira (rotativo) ou quando você parcela a fatura no
banco (aí cadastre esse acordo como dívida do tipo parcelamento).

### Cadastrar (`/novocartao`)

| Pergunta | Exemplo |
| --- | --- |
| Nome | `Nubank` |
| Dia de **fechamento** (o "melhor dia de compra" do app) | `3` |
| Dia de **vencimento** | `10` |
| Limite total | `12.000` |
| Quanto do limite está **usado** hoje (fatura atual + parcelas futuras) | `9.594,54` |
| Valor da fatura **atual** (a que vence no próximo vencimento) | `1.840` |
| Valores das **próximas** faturas, se o app mostrar (opcional) | `600 400 250` |
| Taxa do **rotativo** ao mês, que está na fatura (opcional) | `14,5` |

O que você não informar das próximas faturas fica como **"sem mês definido"**
(parcelas de compras feitas antes do bot): o bot sabe que esse valor ocupa o
limite, mas não em que mês ele cai. Informe cada fatura com `/fatura nome valor`
quando ela fechar e tudo se ajusta.

### Em qual fatura a compra cai

Compra **antes** do dia de fechamento entra na fatura que fecha naquele mês;
compra **no dia do fechamento ou depois** vai para a seguinte. O vencimento é o
primeiro dia de vencimento depois do fechamento. Ex.: fecha dia 3, vence dia 10:
compra em 02/12 vence 10/12; compra em 03/12 vence 10/01.

### Registrar compras

Cite o cartão na mensagem:

```
gastei 80 no mercado no nubank          à vista
comprei tv 3000 em 10x no nubank        10 parcelas de R$ 300 (também: "10 vezes")
45 farmácia nubank                      à vista
```

- A compra soma ao **usado** (a parcelada ocupa o limite inteiro na hora) e entra
  nas faturas certas. Avisos ao passar de **80%, 90% e 100%** do limite.
- **No `/mes`:** compra à vista conta na data da compra; compra parcelada conta
  **uma parcela por mês**, na data de vencimento de cada fatura.
- **`/parcelado`:** cadastro guiado para compras grandes, com o valor de cada
  parcela e (opcional) o preço à vista: o bot mostra os **juros embutidos** e a
  taxa mensal equivalente (ex.: R$ 3.000 à vista em 10x de R$ 330 = R$ 300 de
  juros, ~1,77% a.m.).
- "paguei 50 no mercado no nubank" é ambíguo (compra ou pagamento da fatura?):
  o bot pergunta com botões, incluindo **"Gasto comum (sem cartão)"**.

### Pagar a fatura

- `paguei 1500 da fatura do nubank` (ou `paguei 1500 nubank`) abate a fatura
  atual (e vencidas, se houver; se sobrar, adianta as próximas) e libera o limite.
- **Não vira gasto no `/mes`**: as compras já foram contadas. (Pagamentos das
  dívidas, como empréstimos e acordos, continuam virando gasto.)
- Se a fatura atual ficar incompleta, o bot responde com **🚨 ATENÇÃO: ROTATIVO**:
  quanto falta e quanto isso vira em um mês com a taxa do rotativo.

### Rotativo automático

No dia seguinte ao vencimento, se a fatura não foi paga inteira, o bot lança na
fatura seguinte o que faltou **mais os juros** do rotativo e avisa. Ex.:
faltaram R$ 500 a 15% a.m. → +R$ 575 na próxima fatura (e +R$ 75 no usado).
IOF e encargos não entram na estimativa: o valor real, informado com `/fatura`,
corrige.

### Consultar e corrigir

- `/cartoes`: limite, usado, disponível, fatura atual e próxima de cada cartão,
  e o **melhor cartão para comprar hoje** (aquele em que a compra vence mais tarde).
- `/cartao nome`: detalhes, lançamentos da fatura atual, próximas faturas, o
  "sem mês definido" e os parcelamentos em andamento.
- `/fatura nome` mostra a fatura atual; `/fatura nome valor` grava o valor que o
  **banco** cobrou (substitui o calculado); `/fatura nome valor 10/01` faz isso
  na fatura que vence em 10/01.
- `/editarcartao nome`: nome, fechamento, vencimento, limite, usado (ajuste pelo
  app do banco) e taxa do rotativo.
- `/estorno`: escolhe a compra pelos botões; **total** (parcelas futuras somem, o
  já cobrado vira crédito na fatura aberta, o gasto sai do `/mes`) ou **parcial**
  (crédito do valor na fatura aberta, descontado do gasto). Pede confirmação e
  **não** pode ser desfeito.
- `/desfazer` desfaz a última compra ou pagamento no cartão (faturas, limite e
  gastos juntos), respeitando a ordem com os gastos comuns.
- Lembrete da fatura 3 dias antes do vencimento, se houver algo a pagar.

## Dívidas

### Cadastrar

Mande `/novadivida` e responda uma pergunta por vez:

| Campo | Obrigatório | Exemplo |
| --- | --- | --- |
| Nome | sim | `Empréstimo Nubank` |
| Tipo | sim | botões: empréstimo, financiamento, cheque especial, parcelamento de compra, dívida com pessoa (cartão: use `/novocartao`) |
| Credor | não | o **nome**: `Nubank`, `Casas Bahia`, `Tia Rosa` |
| Saldo devedor atual | sim | `4.200,00` |
| Taxa de juros **ao mês** | não (vazio = sem juros) | `2,5` ou `2,5%`; se souber só a anual: `30% a.a.` (é convertida) |
| Parcelas restantes | não (não perguntado para cheque especial) | `24` |
| Parcela / pagamento mensal | não | `450`; se você informou as parcelas e pular, o bot calcula pela tabela Price |
| Dia de vencimento | não | `10` |
| Data de início | não | `15/03/2025`, `03/2025` ou `hoje` |

Os campos opcionais têm o botão **Pular** (ou escreva `pular`). Antes de salvar
aparece um resumo com a projeção. `/cancelar` sai a qualquer momento sem salvar.

### Editar

`/editardivida nome` (ou só `/editardivida`, para escolher numa lista) abre um
menu com cada campo e o valor atual: nome, tipo, credor, taxa, parcelas
restantes, parcela, vencimento e início. Toque no campo, responda (mesmas
regras do cadastro) e a mudança é salva na hora, mostrando antes → depois e a
nova projeção. O menu volta até você tocar em **✅ Concluir**.

- O botão de apagar deixa o campo em branco. Na taxa, isso significa "sem juros";
  na parcela, se houver parcelas restantes, o bot recalcula pela tabela Price.
- O **saldo** não aparece no menu: corrija com `/saldo`, que fica no histórico.
- Mudar para cheque especial apaga as "parcelas restantes" (esse tipo não tem).
- Não dá para editar e cadastrar ao mesmo tempo: termine um ou use `/cancelar`.
- `/removerdivida nome` apaga uma dívida, depois de confirmar com um botão. O
  histórico dela some; os gastos dos pagamentos continuam no `/mes`.

### Pagar e corrigir

- **Texto livre:** `paguei 450 do empréstimo nubank`. O bot procura a dívida pelo
  nome; senão pelo credor (`paguei 450 nubank`); senão pelo tipo
  (`paguei 900 do financiamento`). Se mais de uma combinar, ou se a mensagem tiver
  palavras que não são da dívida (`paguei 100 de gasolina do carro`), ele
  pergunta com botões, incluindo **"Não, é um gasto comum"**. Só os verbos de
  pagar contam (*paguei, quitei, amortizei, abati*): `gastei 50 no mercado`
  nunca vira pagamento de dívida.
- **Comando:** `/pagar nome valor`.
- O pagamento abate o saldo **e** vira um gasto na categoria `dívidas`
  (aparece no `/mes`, no `/exportar` e pode ter orçamento).
- **Juros:** no primeiro pagamento de cada mês, o saldo recebe os juros do mês
  antes de abater (`saldo × (1 + taxa) − pagamento`). Pagamentos extras no mesmo
  mês abatem direto. Pagar menos que os juros faz o saldo subir, e o bot avisa.
- **Correção:** o banco calcula juros por dia, então o saldo real pode diferir um
  pouco. Confira no app do banco e use `/saldo nome valor`. A correção fica no
  histórico como "ajuste manual". `/saldo nome 0` marca a dívida como quitada.
- **Desfazer:** `/desfazer` num pagamento apaga o gasto e devolve o saldo e as
  parcelas. Se o saldo da dívida mudou depois (por `/saldo`), o bot recusa,
  para não apagar a correção sem você perceber.
- Saldo zerado marca a dívida como **quitada**, exceto cheque especial, que fica
  **em dia** (o limite continua lá).
- Cheque especial sem pagamento definido mostra quanto a dívida cresce por mês
  só de juros (ex.: R$ 1.286,40 a 8% = +R$ 102,91/mês).

### Consultar e projetar

- `/dividas`: todas as dívidas ativas, da maior taxa para a menor, com saldo,
  parcela, taxa, data estimada de quitação e juros a pagar; no fim, os totais.
- `/divida nome`: detalhes, taxa anual equivalente, juros do mês, projeção e os
  últimos 10 eventos do histórico. Se as parcelas restantes do contrato não
  batem com a conta, o bot avisa para conferir saldo e taxa.
- Quando a parcela não cobre os juros do mês, aparece em destaque:
  *"a dívida CRESCE R$ X por mês"*.
- `/simular nome 200`: compara pagar o de sempre com pagar R$ 200 a mais por mês:
  data de quitação, meses e juros economizados.
- `/estrategia` (ou `/estrategia 300` com um extra): compara **avalanche** (maior
  juros primeiro) e **bola de neve** (menor saldo primeiro) com o mesmo orçamento
  mensal: ordem de quitação, data de cada uma, data final e total de juros.
- `/grafico nome`: imagem com o histórico do saldo (linha cheia) e a projeção
  (tracejada; vermelha se a dívida está crescendo), até 10 anos à frente.

### Saúde financeira (`/saude`)

| Indicador | Como é calculado |
| --- | --- |
| Base | média dos 3 últimos meses **completos** com registros (sem nenhum: o mês atual até hoje, avisando que é parcial) |
| Renda | média das entradas |
| Gastos fixos | média das categorias de `FIXED_CATEGORIES` |
| Parcelas | soma do pagamento mensal das dívidas ativas com saldo (cartões ficam de fora: as compras já estão nos gastos) |
| Cartões | linha à parte: usado de todos os limites, faturas atuais a pagar e o que estiver vencido |
| Comprometimento | parcelas ÷ renda → 🟢 saudável / 🟡 atenção / 🔴 alerta, pelas faixas do `.env` |
| Sobra | renda − gastos fixos − parcelas |
| Total em dívidas | soma dos saldos |

**Sugestões** (sempre com os números que as geraram): cada categoria no último
mês completo é comparada com a média dos 3 meses anteriores. As que cresceram
acima dos limites do `.env` aparecem, e o bot calcula quantos meses e quantos
reais de juros você economizaria mandando parte desse aumento para a dívida de
juros mais altos. A categoria `dívidas` (pagamentos) fica de fora.

### Lembretes

- Todo dia às `REMINDER_HOUR`, o bot avisa as dívidas que vencem em até
  `REMINDER_DAYS_BEFORE` dias (padrão: 9h, 3 dias antes), com o valor da parcela,
  e as faturas de cartão com algo a pagar. No mesmo horário roda o rotativo
  automático das faturas vencidas.
- Cada vencimento é avisado uma vez. Se o bot estava desligado no dia certo, o
  aviso sai logo que ele ligar (desde que ainda não tenha vencido).
- Não avisa se você já registrou um pagamento nos 25 dias antes do vencimento,
  nem se a fatura do cartão já está paga.
- Lembretes e rotativo automático dependem do **bot rodando**: desligado, nada é
  enviado; ao religar, ele lança o que ficou para trás.
- Só dívidas com **dia de vencimento** cadastrado geram lembrete.
- `/lembretes` lista os próximos vencimentos e manda na hora os avisos pendentes.

### Fórmulas

Dinheiro em centavos e taxas em milionésimos (inteiros), juros arredondados
para o centavo a cada mês (meio centavo para cima), como os bancos fazem.

| O quê | Fórmula |
| --- | --- |
| Juros do mês | `J = P × i` |
| Saldo após um mês | `P' = P × (1 + i) − A` |
| Parcela Price | `A = P × i / (1 − (1 + i)^−n)` (sem juros: `P / n`) |
| Meses até quitar | `n = −ln(1 − P·i/A) / ln(1 + i)`, só se `A > P·i` |
| Taxa anual equivalente | `(1 + i)^12 − 1` |
| Anual → mensal | `(1 + i_anual)^(1/12) − 1` |

As projeções simulam mês a mês (para tratar a última parcela menor, extras e
estratégias); a fórmula fechada confere a simulação nos testes. Os testes
comparam tudo com exemplos calculados à mão, por exemplo: R$ 10.000 a 2% em
12 parcelas = **R$ 945,60**; R$ 5.000 a 3% pagando R$ 300 = **24 pagamentos**;
rotativo de 15% a.m. = **435% a.a.**

## Seus dados

- Tudo fica em `data/finbot.db` (SQLite), no seu computador. A pasta é criada
  na primeira execução e está no `.gitignore`.
- **Backup manual** (faça antes de atualizar o bot): pare o bot e rode
  `Copy-Item data\finbot.db "data\finbot-backup-$(Get-Date -Format yyyyMMdd-HHmm).db"`.
  Para restaurar, pare o bot e copie a cópia de volta para `data\finbot.db`.
  O `/exportar` também serve de backup legível dos gastos e entradas.
- **Migrações e backup automático**: o esquema do banco evolui por arquivos
  numerados em `src/finbot/db/migrations/`. Ao iniciar, o bot aplica os que
  faltam, cada um numa transação (se der erro, nada muda), e **antes** copia o
  banco para `data/backups/finbot-AAAAMMDD-HHMMSS-vN.db`. Migrações só
  adicionam tabelas e colunas: nunca apagam dados.
- Para ver ou editar os dados à mão, use um programa como o
  [DB Browser for SQLite](https://sqlitebrowser.org/).
- O bot ignora em silêncio qualquer chat que não seja o `ALLOWED_CHAT_ID`.
- O token não aparece nos logs: as mensagens são filtradas e o token vira `***`.

## Problemas comuns

**`Erro de configuração: TELEGRAM_TOKEN não definido`**
Falta o `.env`, ou o comando não foi rodado na pasta do projeto.

**O bot não responde nada**
Quase sempre é o `ALLOWED_CHAT_ID` errado. Olhe o terminal: deve aparecer
`Atualização ignorada de chat não autorizado: chat_id=...`. Copie esse número
para o `.env` e reinicie.

**`Erro de rede ao falar com o Telegram (Timed out)`**
O bot já espera até 20 s e tenta conectar de novo 5 vezes antes de desistir.
Se mesmo assim falhar, verifique internet, VPN, proxy, firewall ou antivírus.
Teste no PowerShell: `Invoke-WebRequest https://api.telegram.org -UseBasicParsing`.

**`Erro: o Telegram recusou o TELEGRAM_TOKEN`**
Token digitado errado ou revogado. Confira no @BotFather: `/mybots` → seu bot → *API Token*.

**`Conflict: terminated by other getUpdates request`**
Há outra cópia do bot rodando (em outro terminal ou computador). Feche-a.

**`ModuleNotFoundError: No module named 'finbot'`**
Reinstale: `.\.venv\Scripts\python.exe -m pip install -e ".[dev]"`.

**Uma mensagem caiu na categoria errada**
Acrescente a palavra em `src/finbot/parsing/categories.py`, ou use `#categoria`.

**`Erro ... falha na migração`**
O banco ficou na versão anterior (a migração é desfeita inteira) e a mensagem
mostra onde está o backup. Não apague nada; guarde a mensagem para investigar.

**O pagamento foi para a dívida errada / virou gasto comum**
`/desfazer` e mande de novo com o nome completo da dívida, ou use
`/pagar nome valor`.

**O saldo calculado está diferente do app do banco**
Normal: o banco calcula juros por dia e pode cobrar encargos. Use
`/saldo nome valor` sempre que conferir.

**Não chegou lembrete**
A dívida precisa ter dia de vencimento cadastrado, e o bot precisa estar
rodando. Veja os próximos vencimentos com `/lembretes`.

## Desenvolvimento

```powershell
.\.venv\Scripts\python.exe -m pytest           # testes
.\.venv\Scripts\python.exe -m ruff check .     # lint
.\.venv\Scripts\python.exe -m ruff format .    # formatação
```

Os testes não acessam a rede nem o Telegram: usam um banco SQLite em memória
e datas fixas.

### Estrutura

```
src/finbot/
├── __main__.py         ponto de entrada (python -m finbot), logs e erros de rede
├── config.py           lê e valida o .env
├── money.py            texto <-> centavos/taxas e formatação em R$ e %
├── timeutil.py         datas no formato do banco (UTC)
├── db/
│   ├── __init__.py     conexão SQLite, migrações e backup automático
│   └── migrations/     001_base.sql, 002_dividas.sql, ...
├── parsing/            entende o texto (sem depender do Telegram)
│   ├── message.py      "gastei 35 no mercado" -> ParsedEntry
│   ├── payment.py      "paguei 450 do empréstimo" + argumentos de /pagar e /saldo
│   ├── debt.py         respostas do cadastro de dívida (taxa, data, dia...)
│   ├── card.py         argumentos do /fatura
│   ├── card_text.py    "gastei 80 no mercado no nubank", "em 10x"
│   ├── categories.py   palavras-chave das categorias
│   ├── period.py       argumento de mês ("10/2025")
│   ├── budget.py       argumentos do /orcamento
│   └── text.py         normalização (minúsculas, sem acento)
├── services/           regras de negócio (sem depender do Telegram)
│   ├── finance.py      CÁLCULOS: juros, Price, projeção, simulação, estratégias
│   ├── debts.py        cadastro, pagamentos, ajustes, desfazer, busca
│   ├── card_math.py    CÁLCULOS do cartão: ciclo da fatura, parcelas, juros embutidos
│   ├── credit_cards.py cartões: faturas, compras, pagamento, estorno, rotativo
│   ├── health.py       indicadores e sugestões do /saude
│   ├── evolution.py    séries do gráfico (histórico + projeção)
│   ├── reminders.py    quais vencimentos avisar
│   ├── transactions.py gravar, listar, apagar, resumir
│   ├── budgets.py      orçamentos e alertas
│   └── export.py       geração do CSV
└── bot/                camada do Telegram
    ├── app.py          monta a aplicação, registra comandos e tarefas agendadas
    ├── handlers.py     gastos, /hoje, /mes, /orcamento, /exportar, /desfazer
    ├── debts.py        /novadivida (fluxo de perguntas), /dividas, /divida
    ├── debt_form.py    roteiro das perguntas do cadastro (testável sem Telegram)
    ├── debt_edit.py    /editardivida (menu de campos)
    ├── payments.py     pagamentos por texto, /pagar, /saldo
    ├── card_form.py    roteiro das perguntas do /novocartao
    ├── card_setup.py   /novocartao e /editarcartao
    ├── card_views.py   /cartoes, /cartao e /fatura
    ├── card_ops.py     compras, pagamento, /parcelado, /estorno, rotativo
    ├── strategy.py     /simular e /estrategia
    ├── health.py       /saude
    ├── chart.py        /grafico (matplotlib)
    ├── reminders.py    lembretes automáticos e /lembretes
    ├── texts.py        textos compartilhados (alertas de orçamento)
    └── security.py     bloqueia chats não autorizados
```

### Mudar o banco

Crie `src/finbot/db/migrations/NNN_descricao.sql` com o próximo número. Só
adicione (tabelas, índices, colunas com valor padrão); nunca apague ou reescreva
dados. O bot aplica e faz backup sozinho na próxima vez que iniciar.

### Adicionar um comando

1. Lógica nova em `services/` (e, se precisar ler argumentos, em `parsing/`),
   com testes.
2. Um handler "fino" em `bot/handlers.py`: lê a mensagem, chama o serviço,
   formata a resposta.
3. Registre em `bot/app.py` com `CommandHandler("nome", handlers.funcao)`.
4. Acrescente na `HELP_TEXT`.

Não é preciso se preocupar com segurança no handler novo: o filtro de
`security.py` roda antes de todos.

## Decisões de projeto

- **Dinheiro em centavos (int)**: R$ 12,50 é guardado como `1250`. Evita erros
  de arredondamento de `float` (`0.1 + 0.2 != 0.3`).
- **Datas em UTC no banco**: convertidas para o `TIMEZONE` só para exibir e para
  calcular "hoje"/"mês". Trocar o fuso não corrompe nada.
- **SQLite**: um arquivo só, já vem com o Python, sem servidor para manter.
- **Long polling**: o bot pergunta ao Telegram se há mensagens novas. Não precisa
  de IP público nem HTTPS, ideal para rodar em casa.
- **Um filtro de segurança global** (grupo -1) em vez de checar o chat em cada
  handler: um comando novo nunca fica desprotegido por esquecimento.
- **Handlers finos**: `parsing/` e `services/` não conhecem o Telegram, então
  quase tudo é testável sem rede.
- **Categorias por palavras-chave**: previsível, editável e offline.
- **Alertas de orçamento só ao cruzar 80%/100%**: avisar a cada gasto viraria ruído.
- **Taxas em milionésimos (int)**: 1,99% a.m. é guardado como `19900`; os juros
  do mês são calculados só com inteiros.
- **Cálculos financeiros isolados** em `services/finance.py`, sem banco nem
  Telegram, testados contra exemplos feitos à mão: um erro nos juros invalidaria
  todas as projeções.
- **Juros lançados no primeiro pagamento do mês**: reproduz o sistema Price e
  mantém o saldo coerente com o do banco; `/saldo` corrige as diferenças.
- **Pagamento = gasto + evento + saldo numa transação só**: nunca fica metade gravada.
- **Histórico em `debt_events`**: cada cadastro, pagamento, ajuste e fatura guarda
  o saldo antes e depois; é a fonte do `/divida` e do `/grafico`.
- **Cartão separado das dívidas**: cartão é meio de pagamento; só o rotativo e
  os acordos de fatura são dívida. As compras viram gastos e montam as faturas
  pelo dia de fechamento; pagar a fatura não é gasto (senão contaria duas vezes).
- **Fatura = soma dos lançamentos, ou o valor do banco**: o banco sabe de coisas
  que o bot não vê (anuidade, IOF, compras não registradas), então o valor
  informado com `/fatura` sempre prevalece.
- **Cheque especial nunca "quita"**: zerado, fica "em dia".
- **Sugestões só com regras transparentes**: nada de conselho genérico; cada
  sugestão mostra os números de onde veio.
