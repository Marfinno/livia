# 🤖 Bot de Vendas VIP – Deploy no Railway

---

## ⚡ Passo a passo no Railway (5 minutos)

### 1. Suba o código para o GitHub

```bash
git init
git add bot.py requirements.txt .env.example
git commit -m "vip bot inicial"
git remote add origin https://github.com/SEU_USER/SEU_REPO.git
git push -u origin main
```

> Nunca faça commit do arquivo `.env` — ele fica só na sua máquina e no Railway.

---

### 2. Crie o projeto no Railway

1. Acesse [railway.app](https://railway.app) → **New Project**
2. Clique em **Deploy from GitHub repo**
3. Autorize e escolha seu repositório

---

### 3. Configure as variáveis de ambiente

No Railway, vá em **Variables** e adicione uma por uma:

| Variável | Valor |
|---|---|
| `BOT_TOKEN` | Token do @BotFather |
| `ADMIN_ID` | Seu ID numérico do Telegram |
| `VEXOPAY_API_KEY` | Chave da VexoPay |
| `VEXOPAY_SECRET` | Secret da VexoPay |
| `WEBHOOK_SECRET` | Qualquer string forte aleatória |
| `BASE_URL` | *preencher depois (passo 5)* |
| `USE_WEBHOOK` | `true` |
| `DATABASE_PATH` | `vip_bot.db` |

> `PORT` **não precisa adicionar** — o Railway injeta automaticamente.

---

### 4. Configure o start command

Em **Settings → Deploy → Start Command**:

```
python bot.py
```

---

### 5. Gere o domínio público

Em **Settings → Domains → Generate Domain**

Você vai receber algo como:
```
https://meu-bot-production.up.railway.app
```

Agora volte em **Variables** e adicione:
```
BASE_URL=https://meu-bot-production.up.railway.app
```

Faça um **Redeploy** para aplicar.

---

### 6. Confirme que o webhook está ativo

Acesse no navegador:
```
https://SEU_DOMINIO.up.railway.app/webhook/info
```

Você deve ver:
```json
{
  "url": "https://SEU_DOMINIO.up.railway.app/telegram/webhook",
  "pending_update_count": 0,
  "last_error_message": null
}
```

Se `url` estiver vazio, faça um Redeploy ou acesse:
```
DELETE https://SEU_DOMINIO.up.railway.app/webhook/reset
```

---

### 7. Configure o webhook da VexoPay

No painel da VexoPay, defina a URL de notificação como:

```
https://SEU_DOMINIO.up.railway.app/webhook/vexopay
```

---

## 📍 Endpoints do servidor

| Método | Caminho | Função |
|---|---|---|
| `POST` | `/telegram/webhook` | Recebe updates do Telegram |
| `POST` | `/webhook/vexopay` | Recebe confirmação de pagamento |
| `GET` | `/health` | Health check (UptimeRobot) |
| `GET` | `/webhook/info` | Status do webhook no Telegram |
| `DELETE` | `/webhook/reset` | Remove e re-registra o webhook |

---

## 🔁 Como o fluxo de webhook funciona

```
Usuário manda /start
       ↓
Telegram → POST /telegram/webhook → aiogram processa → bot responde

Usuário clica no pacote
       ↓
bot chama VexoPay API → recebe PIX → envia QR Code pro usuário

Usuário paga o PIX
       ↓
VexoPay → POST /webhook/vexopay → bot entrega o conteúdo automaticamente
```

---

## 💾 Persistência do banco de dados

> ⚠️ O Railway **não persiste arquivos** entre deploys por padrão.
> O `vip_bot.db` some a cada novo deploy.

**Solução recomendada:** adicione um **Volume** no Railway:

1. No projeto → **+ New** → **Volume**
2. Mount path: `/data`
3. Mude `DATABASE_PATH` para `/data/vip_bot.db` nas variáveis

Pronto, o banco sobrevive a qualquer redeploy.

---

## 🛠️ Desenvolvimento local (sem domínio)

```bash
# Instalar dependências
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Configurar .env
cp .env.example .env
# edite: BOT_TOKEN, ADMIN_ID
# deixe BASE_URL vazio → bot usará polling automático

# Rodar
python bot.py
```

Para testar webhooks localmente, use o **ngrok**:
```bash
ngrok http 8000
# copie a URL https://xxxx.ngrok.io para BASE_URL no .env
```

---

## 🔒 Segurança

- Webhook do Telegram protegido por `secret_token` (header `X-Telegram-Bot-Api-Secret-Token`)
- Webhook da VexoPay protegido por HMAC SHA-256 (header `X-Vexopay-Signature`)
- Admin protegido por verificação de `ADMIN_ID` em todos os handlers
- Nunca suba o `.env` para o repositório público

---

## 👑 Comandos do Admin

```
/admin          → painel com botões
/setwelcome     → define foto + mensagem de boas-vindas
/addpack        → adiciona novo pacote VIP
/listpacks      → lista pacotes ativos
/deletepack     → desativa um pacote
/savecontent    → salva mídias (foto/vídeo/áudio/arquivo/GIF)
/linkcontent    → vincula conteúdo salvo a um pacote
/listcontent    → lista todos os conteúdos salvos
/stats          → estatísticas de vendas
/done           → finaliza o modo savecontent
```
