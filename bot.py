"""
╔══════════════════════════════════════════════════════════════╗
║           BOT DE VENDAS VIP – TELEGRAM + VEXOPAY             ║
║         Modo WEBHOOK (Railway / VPS com domínio)             ║
╚══════════════════════════════════════════════════════════════╝

COMO FUNCIONA NO RAILWAY:
  - FastAPI recebe updates do Telegram em POST /telegram/webhook
  - FastAPI recebe confirmações de pagamento em POST /webhook/vexopay
  - Não usa polling — ideal para produção
  - Railway injeta a variável PORT automaticamente
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

import aiohttp
import uvicorn
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

# ──────────────────────────────────────────────
# VARIÁVEIS DE AMBIENTE
# ──────────────────────────────────────────────
load_dotenv()

BOT_TOKEN        = os.getenv("BOT_TOKEN", "")
ADMIN_ID         = int(os.getenv("ADMIN_ID", "0"))
VEXOPAY_API_KEY  = os.getenv("VEXOPAY_API_KEY", "")
VEXOPAY_SECRET   = os.getenv("VEXOPAY_SECRET", "")
WEBHOOK_SECRET   = os.getenv("WEBHOOK_SECRET", "")
BASE_URL         = os.getenv("BASE_URL", "").rstrip("/")
DATABASE_PATH    = os.getenv("DATABASE_PATH", "vip_bot.db")
PORT             = int(os.getenv("PORT", "8000"))   # Railway injeta PORT
USE_WEBHOOK      = os.getenv("USE_WEBHOOK", "true").lower() == "true"
VEXOPAY_BASE_URL = "https://api.vexopay.com.br/v1"

# Caminho que o Telegram vai chamar via POST
TG_WEBHOOK_PATH = "/telegram/webhook"

# ──────────────────────────────────────────────
# LOGGING
# ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%d/%m/%Y %H:%M:%S",
)
log = logging.getLogger("VIPBot")

# ──────────────────────────────────────────────
# BANCO DE DADOS
# ──────────────────────────────────────────────
def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    ddl = """
    CREATE TABLE IF NOT EXISTS users (
        telegram_id   INTEGER PRIMARY KEY,
        username      TEXT,
        first_name    TEXT,
        first_seen    TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS packs (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        name          TEXT    NOT NULL,
        description   TEXT,
        price         REAL    NOT NULL,
        delivery_type TEXT    NOT NULL DEFAULT 'media',
        invite_link   TEXT,
        active        INTEGER NOT NULL DEFAULT 1,
        created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS contents (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_file_id TEXT  NOT NULL,
        file_type        TEXT  NOT NULL,
        caption          TEXT,
        created_at       TEXT  NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS pack_contents (
        pack_id    INTEGER NOT NULL REFERENCES packs(id)    ON DELETE CASCADE,
        content_id INTEGER NOT NULL REFERENCES contents(id) ON DELETE CASCADE,
        PRIMARY KEY (pack_id, content_id)
    );
    CREATE TABLE IF NOT EXISTS payments (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id    INTEGER NOT NULL,
        pack_id        INTEGER NOT NULL,
        amount         REAL    NOT NULL,
        status         TEXT    NOT NULL DEFAULT 'pending',
        transaction_id TEXT    UNIQUE,
        pix_copy_paste TEXT,
        qr_code_base64 TEXT,
        created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
        paid_at        TEXT
    );
    CREATE TABLE IF NOT EXISTS bot_config (
        key   TEXT PRIMARY KEY,
        value TEXT
    );
    INSERT OR IGNORE INTO bot_config (key, value) VALUES
        ('welcome_photo_id',  ''),
        ('welcome_caption',   '👑 Bem-vindo(a) ao nosso sistema VIP!\n\nEscolha seu plano abaixo e tenha acesso imediato ao conteúdo exclusivo.'),
        ('payment_message',   '✅ Pagamento gerado!\n\n💰 Valor: R$ {valor}\n📦 Pacote: {pacote}\n\nEscaneie o QR Code ou copie o Pix abaixo:');
    """
    with get_connection() as conn:
        conn.executescript(ddl)
    log.info("✅ Banco de dados inicializado.")


# ── helpers de DB ──────────────────────────────
def db_get_config(key: str) -> str:
    with get_connection() as conn:
        row = conn.execute("SELECT value FROM bot_config WHERE key=?", (key,)).fetchone()
        return row["value"] if row else ""

def db_set_config(key: str, value: str) -> None:
    with get_connection() as conn:
        conn.execute("INSERT OR REPLACE INTO bot_config (key, value) VALUES (?,?)", (key, value))
        conn.commit()

def db_upsert_user(telegram_id, username, first_name) -> None:
    with get_connection() as conn:
        conn.execute("INSERT OR IGNORE INTO users (telegram_id, username, first_name) VALUES (?,?,?)",
                     (telegram_id, username, first_name))
        conn.execute("UPDATE users SET username=?, first_name=? WHERE telegram_id=?",
                     (username, first_name, telegram_id))
        conn.commit()

def db_get_active_packs() -> list:
    with get_connection() as conn:
        return conn.execute("SELECT * FROM packs WHERE active=1 ORDER BY price ASC").fetchall()

def db_get_pack(pack_id: int) -> Optional[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute("SELECT * FROM packs WHERE id=?", (pack_id,)).fetchone()

def db_add_pack(name, description, price, delivery_type, invite_link=None) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO packs (name, description, price, delivery_type, invite_link) VALUES (?,?,?,?,?)",
            (name, description, float(price), delivery_type, invite_link))
        conn.commit()
        return cur.lastrowid

def db_delete_pack(pack_id: int) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE packs SET active=0 WHERE id=?", (pack_id,))
        conn.commit()

def db_add_content(file_id, file_type, caption="") -> int:
    with get_connection() as conn:
        cur = conn.execute("INSERT INTO contents (telegram_file_id, file_type, caption) VALUES (?,?,?)",
                           (file_id, file_type, caption))
        conn.commit()
        return cur.lastrowid

def db_list_contents() -> list:
    with get_connection() as conn:
        return conn.execute("SELECT * FROM contents ORDER BY id DESC").fetchall()

def db_link_content(pack_id: int, content_id: int) -> None:
    with get_connection() as conn:
        conn.execute("INSERT OR IGNORE INTO pack_contents (pack_id, content_id) VALUES (?,?)",
                     (pack_id, content_id))
        conn.commit()

def db_get_pack_contents(pack_id: int) -> list:
    with get_connection() as conn:
        return conn.execute(
            "SELECT c.* FROM contents c JOIN pack_contents pc ON pc.content_id=c.id WHERE pc.pack_id=?",
            (pack_id,)).fetchall()

def db_create_payment(telegram_id, pack_id, amount) -> int:
    with get_connection() as conn:
        cur = conn.execute("INSERT INTO payments (telegram_id, pack_id, amount) VALUES (?,?,?)",
                           (telegram_id, pack_id, amount))
        conn.commit()
        return cur.lastrowid

def db_update_payment(payment_id, transaction_id, pix, qr) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE payments SET transaction_id=?, pix_copy_paste=?, qr_code_base64=? WHERE id=?",
            (transaction_id, pix, qr, payment_id))
        conn.commit()

def db_mark_payment_paid(transaction_id: str) -> Optional[sqlite3.Row]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM payments WHERE transaction_id=? AND status='pending'",
            (transaction_id,)).fetchone()
        if row:
            conn.execute("UPDATE payments SET status='paid', paid_at=datetime('now') WHERE id=?", (row["id"],))
            conn.commit()
        return row

def db_stats() -> dict:
    with get_connection() as conn:
        u = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
        p = conn.execute("SELECT COUNT(*) as c FROM packs WHERE active=1").fetchone()["c"]
        s = conn.execute("SELECT COUNT(*) as c FROM payments WHERE status='paid'").fetchone()["c"]
        r = conn.execute("SELECT COALESCE(SUM(amount),0) as v FROM payments WHERE status='paid'").fetchone()["v"]
        return {"users": u, "packs": p, "sales": s, "revenue": r}


# ──────────────────────────────────────────────
# VEXOPAY
# ──────────────────────────────────────────────
async def vexopay_create_pix(amount, customer_name, customer_id, description, payment_id) -> dict:
    headers = {
        "Authorization": f"Bearer {VEXOPAY_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "amount": int(amount * 100),
        "payment_method": "pix",
        "customer": {"name": customer_name, "external_id": str(customer_id)},
        "description": description,
        "metadata": {"telegram_user_id": str(customer_id), "payment_id": str(payment_id)},
        "notification_url": f"{BASE_URL}/webhook/vexopay",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{VEXOPAY_BASE_URL}/transactions", headers=headers,
                                    json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                data = await resp.json()
                if resp.status in (200, 201):
                    return {
                        "transaction_id": data.get("id", str(uuid.uuid4())),
                        "pix_copy_paste": data.get("pix", {}).get("copy_paste", ""),
                        "qr_code_base64": data.get("pix", {}).get("qr_code", ""),
                    }
                log.warning("VexoPay status %s – usando simulação", resp.status)
    except Exception as exc:
        log.error("Erro VexoPay: %s", exc)

    # simulação local (desenvolvimento / sem chave real)
    fake_tx  = str(uuid.uuid4()).replace("-", "")[:24].upper()
    fake_pix = (f"00020101021126580014br.gov.bcb.pix0136{fake_tx}"
                f"5204000053039865406{int(amount*100):08d}5802BR5913VIPBot"
                f"6008Brasilia62070503***6304ABCD")
    return {"transaction_id": fake_tx, "pix_copy_paste": fake_pix, "qr_code_base64": ""}


# ──────────────────────────────────────────────
# FSM – ESTADOS
# ──────────────────────────────────────────────
class AdminStates(StatesGroup):
    aguardando_foto_boas_vindas = State()
    nome_pack       = State()
    preco_pack      = State()
    descricao_pack  = State()
    tipo_entrega    = State()
    link_convite    = State()
    salvando_conteudo = State()
    link_pack_id    = State()
    link_content_id = State()
    deletar_pack_id = State()


# ──────────────────────────────────────────────
# INSTÂNCIAS GLOBAIS
# ──────────────────────────────────────────────
bot: Bot = None
dp  = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)


# ──────────────────────────────────────────────
# TECLADOS
# ──────────────────────────────────────────────
def kb_packs(packs) -> InlineKeyboardMarkup:
    rows = []
    for p in packs:
        emoji = "🥉" if p["price"] < 30 else ("🥇" if p["price"] < 60 else "💎")
        rows.append([InlineKeyboardButton(
            text=f"{emoji} {p['name']} — R$ {p['price']:.2f}",
            callback_data=f"pack:{p['id']}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_delivery_type() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📁 Mídia salva (fotos/vídeos/arquivos)", callback_data="dtype:media")],
        [InlineKeyboardButton(text="🔗 Link de convite (canal/grupo privado)", callback_data="dtype:invite")],
        [InlineKeyboardButton(text="🎁 Misto (mídia + convite)", callback_data="dtype:mixed")],
    ])

def kb_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Listar Pacotes",    callback_data="admin:listpacks"),
         InlineKeyboardButton(text="➕ Adicionar Pacote",  callback_data="admin:addpack")],
        [InlineKeyboardButton(text="🖼️ Salvar Conteúdo",  callback_data="admin:savecontent"),
         InlineKeyboardButton(text="🔗 Linkar Conteúdo",  callback_data="admin:linkcontent")],
        [InlineKeyboardButton(text="🗑️ Deletar Pacote",   callback_data="admin:deletepack"),
         InlineKeyboardButton(text="📋 Listar Conteúdos", callback_data="admin:listcontent")],
        [InlineKeyboardButton(text="🎨 Config Boas-Vindas", callback_data="admin:setwelcome"),
         InlineKeyboardButton(text="📊 Estatísticas",     callback_data="admin:stats")],
    ])


# ──────────────────────────────────────────────
# ENTREGA DE CONTEÚDO
# ──────────────────────────────────────────────
async def deliver_pack(telegram_id: int, pack_id: int) -> None:
    pack = db_get_pack(pack_id)
    if not pack:
        log.error("Pack %s não encontrado para entrega.", pack_id)
        return
    await bot.send_message(
        telegram_id,
        f"🎉 *Pagamento confirmado!*\n\n✅ Acesso ao *{pack['name']}* liberado.\nObrigado pela confiança! 💜",
        parse_mode=ParseMode.MARKDOWN)

    if pack["delivery_type"] in ("media", "mixed"):
        for item in db_get_pack_contents(pack_id):
            fid, ftype, cap = item["telegram_file_id"], item["file_type"], item["caption"] or ""
            try:
                senders = {
                    "photo":     bot.send_photo,
                    "video":     bot.send_video,
                    "audio":     bot.send_audio,
                    "document":  bot.send_document,
                    "animation": bot.send_animation,
                }
                send_fn = senders.get(ftype)
                if send_fn:
                    await send_fn(telegram_id, fid, caption=cap)
            except Exception as exc:
                log.error("Erro ao enviar mídia %s: %s", fid, exc)

    if pack["delivery_type"] in ("invite", "mixed") and pack["invite_link"]:
        await bot.send_message(
            telegram_id,
            f"🔗 *Seu link de acesso VIP:*\n{pack['invite_link']}",
            parse_mode=ParseMode.MARKDOWN)

    log.info("✅ Pack %s entregue ao usuário %s", pack_id, telegram_id)


# ──────────────────────────────────────────────
# HANDLERS – USUÁRIO
# ──────────────────────────────────────────────
@router.message(Command("start"))
async def cmd_start(msg: Message):
    db_upsert_user(msg.from_user.id, msg.from_user.username or "", msg.from_user.first_name or "")
    packs    = db_get_active_packs()
    caption  = db_get_config("welcome_caption")
    photo_id = db_get_config("welcome_photo_id")
    kb       = kb_packs(packs) if packs else None
    if photo_id:
        await msg.answer_photo(photo_id, caption=caption, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    else:
        await msg.answer(caption, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    if not packs:
        await msg.answer("ℹ️ Nenhum pacote disponível no momento. Volte em breve!")


@router.callback_query(F.data.startswith("pack:"))
async def cb_pack_selected(cq: CallbackQuery):
    pack = db_get_pack(int(cq.data.split(":")[1]))
    if not pack:
        await cq.answer("Pacote não encontrado.", show_alert=True); return
    await cq.answer()
    user       = cq.from_user
    payment_id = db_create_payment(user.id, pack["id"], pack["price"])
    msg_esp    = await cq.message.answer("⏳ Gerando seu Pix, aguarde...")
    result     = await vexopay_create_pix(
        pack["price"], user.first_name or "Cliente", str(user.id),
        f"VIP {pack['name']}", payment_id)
    db_update_payment(payment_id, result["transaction_id"], result["pix_copy_paste"], result["qr_code_base64"])

    template  = db_get_config("payment_message")
    texto     = template.format(valor=f"{pack['price']:.2f}", pacote=pack["name"])
    pix_block = (f"\n\n📋 *Pix Copia e Cola:*\n`{result['pix_copy_paste']}`"
                 if result["pix_copy_paste"] else "")
    kb_pix    = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔄 Verificar Pagamento", callback_data=f"checkpay:{result['transaction_id']}")
    ]])
    await msg_esp.delete()

    if result["qr_code_base64"]:
        import base64
        await cq.message.answer_photo(
            BufferedInputFile(base64.b64decode(result["qr_code_base64"]), "qrcode.png"),
            caption=f"{texto}{pix_block}", parse_mode=ParseMode.MARKDOWN, reply_markup=kb_pix)
    else:
        await cq.message.answer(
            f"{texto}{pix_block}\n\n_(QR Code indisponível em modo simulação)_",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_pix)


@router.callback_query(F.data.startswith("checkpay:"))
async def cb_check_payment(cq: CallbackQuery):
    tx_id = cq.data.split(":", 1)[1]
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM payments WHERE transaction_id=?", (tx_id,)).fetchone()
    if not row:
        await cq.answer("Pagamento não encontrado.", show_alert=True)
    elif row["status"] == "paid":
        await cq.answer("✅ Pago! Conteúdo já foi enviado.", show_alert=True)
    else:
        await cq.answer("⏳ Ainda não identificamos. Tente em instantes.", show_alert=True)


# ──────────────────────────────────────────────
# HANDLERS – ADMIN
# ──────────────────────────────────────────────
def is_admin(msg: Message) -> bool:
    return msg.from_user.id == ADMIN_ID

@router.message(Command("admin"))
async def cmd_admin(msg: Message):
    if not is_admin(msg): return
    await msg.answer("👑 *Painel Admin*\nEscolha uma opção:", parse_mode=ParseMode.MARKDOWN, reply_markup=kb_admin())

@router.message(Command("setwelcome"))
async def cmd_setwelcome(msg: Message, state: FSMContext):
    if not is_admin(msg): return
    await state.set_state(AdminStates.aguardando_foto_boas_vindas)
    await msg.answer("📸 Envie uma *foto com legenda* (ou só texto) para as boas-vindas:", parse_mode=ParseMode.MARKDOWN)

@router.message(AdminStates.aguardando_foto_boas_vindas)
async def process_welcome(msg: Message, state: FSMContext):
    if msg.photo:
        db_set_config("welcome_photo_id", msg.photo[-1].file_id)
        db_set_config("welcome_caption", msg.caption or db_get_config("welcome_caption"))
        await msg.answer("✅ Foto e legenda de boas-vindas salvas!")
    elif msg.text:
        db_set_config("welcome_caption", msg.text)
        await msg.answer("✅ Mensagem de boas-vindas salva!")
    else:
        await msg.answer("⚠️ Envie foto com legenda ou texto."); return
    await state.clear()

@router.message(Command("addpack"))
async def cmd_addpack(msg: Message, state: FSMContext):
    if not is_admin(msg): return
    await state.set_state(AdminStates.nome_pack)
    await msg.answer("📦 *Novo Pacote*\n\nDigite o *nome* do pacote:", parse_mode=ParseMode.MARKDOWN)

@router.message(AdminStates.nome_pack)
async def addpack_nome(msg: Message, state: FSMContext):
    await state.update_data(nome=msg.text.strip())
    await state.set_state(AdminStates.preco_pack)
    await msg.answer("💰 Digite o *preço* (ex: `39.90`):", parse_mode=ParseMode.MARKDOWN)

@router.message(AdminStates.preco_pack)
async def addpack_preco(msg: Message, state: FSMContext):
    try:
        preco = float(msg.text.replace(",", ".").strip())
    except ValueError:
        await msg.answer("⚠️ Preço inválido. Use `19.90`."); return
    await state.update_data(preco=preco)
    await state.set_state(AdminStates.descricao_pack)
    await msg.answer("📝 Digite a *descrição* do pacote:", parse_mode=ParseMode.MARKDOWN)

@router.message(AdminStates.descricao_pack)
async def addpack_desc(msg: Message, state: FSMContext):
    await state.update_data(descricao=msg.text.strip())
    await state.set_state(AdminStates.tipo_entrega)
    await msg.answer("🚀 Escolha o *tipo de entrega*:", parse_mode=ParseMode.MARKDOWN, reply_markup=kb_delivery_type())

@router.callback_query(StateFilter(AdminStates.tipo_entrega), F.data.startswith("dtype:"))
async def addpack_delivery(cq: CallbackQuery, state: FSMContext):
    delivery = cq.data.split(":")[1]
    await state.update_data(delivery=delivery)
    await cq.answer()
    if delivery in ("invite", "mixed"):
        await state.set_state(AdminStates.link_convite)
        await cq.message.answer("🔗 Envie o *link de convite* do canal/grupo VIP:", parse_mode=ParseMode.MARKDOWN)
    else:
        data = await state.get_data()
        pid  = db_add_pack(data["nome"], data["descricao"], data["preco"], delivery)
        await state.clear()
        await cq.message.answer(
            f"✅ Pacote *{data['nome']}* criado! ID: `{pid}`\n"
            "Use `/savecontent` e `/linkcontent` para adicionar mídias.",
            parse_mode=ParseMode.MARKDOWN)

@router.message(AdminStates.link_convite)
async def addpack_link(msg: Message, state: FSMContext):
    await state.update_data(link=msg.text.strip())
    data = await state.get_data()
    pid  = db_add_pack(data["nome"], data["descricao"], data["preco"], data["delivery"], data["link"])
    await state.clear()
    await msg.answer(f"✅ Pacote *{data['nome']}* criado! ID: `{pid}`", parse_mode=ParseMode.MARKDOWN)

@router.message(Command("savecontent"))
async def cmd_savecontent(msg: Message, state: FSMContext):
    if not is_admin(msg): return
    await state.set_state(AdminStates.salvando_conteudo)
    await msg.answer("📤 Envie mídias (foto/vídeo/áudio/arquivo/GIF).\nQuando terminar, envie /done.", parse_mode=ParseMode.MARKDOWN)

@router.message(AdminStates.salvando_conteudo, F.photo)
async def save_photo(msg: Message, state: FSMContext):
    cid = db_add_content(msg.photo[-1].file_id, "photo", msg.caption or "")
    await msg.answer(f"✅ Foto salva! ID: `{cid}`", parse_mode=ParseMode.MARKDOWN)

@router.message(AdminStates.salvando_conteudo, F.video)
async def save_video(msg: Message, state: FSMContext):
    cid = db_add_content(msg.video.file_id, "video", msg.caption or "")
    await msg.answer(f"✅ Vídeo salvo! ID: `{cid}`", parse_mode=ParseMode.MARKDOWN)

@router.message(AdminStates.salvando_conteudo, F.audio)
async def save_audio(msg: Message, state: FSMContext):
    cid = db_add_content(msg.audio.file_id, "audio", msg.caption or "")
    await msg.answer(f"✅ Áudio salvo! ID: `{cid}`", parse_mode=ParseMode.MARKDOWN)

@router.message(AdminStates.salvando_conteudo, F.document)
async def save_doc(msg: Message, state: FSMContext):
    cid = db_add_content(msg.document.file_id, "document", msg.caption or "")
    await msg.answer(f"✅ Arquivo salvo! ID: `{cid}`", parse_mode=ParseMode.MARKDOWN)

@router.message(AdminStates.salvando_conteudo, F.animation)
async def save_anim(msg: Message, state: FSMContext):
    cid = db_add_content(msg.animation.file_id, "animation", msg.caption or "")
    await msg.answer(f"✅ GIF salvo! ID: `{cid}`", parse_mode=ParseMode.MARKDOWN)

@router.message(AdminStates.salvando_conteudo, Command("done"))
async def save_done(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("✅ Salvamento concluído! Use `/linkcontent` para vincular.", parse_mode=ParseMode.MARKDOWN)

@router.message(Command("linkcontent"))
async def cmd_linkcontent(msg: Message, state: FSMContext):
    if not is_admin(msg): return
    packs = db_get_active_packs()
    if not packs:
        await msg.answer("⚠️ Nenhum pacote cadastrado."); return
    lista = "\n".join(f"  `{p['id']}` — {p['name']}" for p in packs)
    await state.set_state(AdminStates.link_pack_id)
    await msg.answer(f"📦 *Pacotes:*\n{lista}\n\nDigite o *ID do pacote*:", parse_mode=ParseMode.MARKDOWN)

@router.message(AdminStates.link_pack_id)
async def lc_pack_id(msg: Message, state: FSMContext):
    try:
        pack_id = int(msg.text.strip())
    except ValueError:
        await msg.answer("⚠️ ID inválido."); return
    if not db_get_pack(pack_id):
        await msg.answer("⚠️ Pacote não encontrado."); return
    await state.update_data(pack_id=pack_id)
    contents = db_list_contents()
    if not contents:
        await msg.answer("⚠️ Nenhum conteúdo. Use `/savecontent` primeiro.", parse_mode=ParseMode.MARKDOWN)
        await state.clear(); return
    lista = "\n".join(f"  `{c['id']}` — {c['file_type']} | {c['caption'][:30] or '(sem legenda)'}" for c in contents)
    await state.set_state(AdminStates.link_content_id)
    await msg.answer(f"🗂️ *Conteúdos:*\n{lista}\n\nDigite o *ID do conteúdo*:", parse_mode=ParseMode.MARKDOWN)

@router.message(AdminStates.link_content_id)
async def lc_content_id(msg: Message, state: FSMContext):
    try:
        cid = int(msg.text.strip())
    except ValueError:
        await msg.answer("⚠️ ID inválido."); return
    data = await state.get_data()
    db_link_content(data["pack_id"], cid)
    await state.clear()
    await msg.answer(f"✅ Conteúdo `{cid}` vinculado ao pacote `{data['pack_id']}`!", parse_mode=ParseMode.MARKDOWN)

@router.message(Command("listpacks"))
async def cmd_listpacks(msg: Message):
    if not is_admin(msg): return
    packs = db_get_active_packs()
    if not packs:
        await msg.answer("📭 Nenhum pacote ativo."); return
    linhas = [
        f"🆔 `{p['id']}` | *{p['name']}* — R$ {p['price']:.2f}\n   📝 {p['description']}\n   🚀 `{p['delivery_type']}`"
        for p in packs]
    await msg.answer("📦 *Pacotes Ativos:*\n\n" + "\n\n".join(linhas), parse_mode=ParseMode.MARKDOWN)

@router.message(Command("deletepack"))
async def cmd_deletepack(msg: Message, state: FSMContext):
    if not is_admin(msg): return
    packs = db_get_active_packs()
    if not packs:
        await msg.answer("📭 Nenhum pacote."); return
    lista = "\n".join(f"  `{p['id']}` — {p['name']}" for p in packs)
    await state.set_state(AdminStates.deletar_pack_id)
    await msg.answer(f"🗑️ *Pacotes:*\n{lista}\n\nDigite o *ID* a desativar:", parse_mode=ParseMode.MARKDOWN)

@router.message(AdminStates.deletar_pack_id)
async def process_deletepack(msg: Message, state: FSMContext):
    try:
        pid = int(msg.text.strip())
    except ValueError:
        await msg.answer("⚠️ ID inválido."); return
    pack = db_get_pack(pid)
    if not pack:
        await msg.answer("⚠️ Pacote não encontrado."); await state.clear(); return
    db_delete_pack(pid)
    await state.clear()
    await msg.answer(f"✅ Pacote *{pack['name']}* desativado.", parse_mode=ParseMode.MARKDOWN)

@router.message(Command("listcontent"))
async def cmd_listcontent(msg: Message):
    if not is_admin(msg): return
    contents = db_list_contents()
    if not contents:
        await msg.answer("📭 Nenhum conteúdo."); return
    linhas = [f"🆔 `{c['id']}` | *{c['file_type']}* | {c['caption'][:40] or '(sem legenda)'}" for c in contents]
    await msg.answer("🗂️ *Conteúdos:*\n\n" + "\n".join(linhas), parse_mode=ParseMode.MARKDOWN)

@router.message(Command("stats"))
async def cmd_stats(msg: Message):
    if not is_admin(msg): return
    s = db_stats()
    await msg.answer(
        f"📊 *Estatísticas*\n\n"
        f"👤 Usuários: `{s['users']}`\n"
        f"📦 Pacotes ativos: `{s['packs']}`\n"
        f"💳 Vendas confirmadas: `{s['sales']}`\n"
        f"💰 Receita total: `R$ {s['revenue']:.2f}`",
        parse_mode=ParseMode.MARKDOWN)

@router.callback_query(F.data.startswith("admin:"))
async def cb_admin_panel(cq: CallbackQuery, state: FSMContext):
    if cq.from_user.id != ADMIN_ID:
        await cq.answer("⛔ Acesso negado.", show_alert=True); return
    action = cq.data.split(":")[1]
    await cq.answer()
    handlers_com_state = {"addpack", "savecontent", "linkcontent", "deletepack", "setwelcome"}
    dispatch = {
        "listpacks":   cmd_listpacks,
        "addpack":     cmd_addpack,
        "savecontent": cmd_savecontent,
        "linkcontent": cmd_linkcontent,
        "deletepack":  cmd_deletepack,
        "listcontent": cmd_listcontent,
        "setwelcome":  cmd_setwelcome,
        "stats":       cmd_stats,
    }
    fn = dispatch.get(action)
    if fn:
        if action in handlers_com_state:
            await fn(cq.message, state)
        else:
            await fn(cq.message)


# ──────────────────────────────────────────────
# FASTAPI – LIFESPAN
# ──────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot
    init_db()
    bot = Bot(token=BOT_TOKEN)

    if USE_WEBHOOK and BASE_URL:
        # ── Modo WEBHOOK (Railway / VPS) ──
        webhook_url = f"{BASE_URL}{TG_WEBHOOK_PATH}"
        await bot.set_webhook(
            url=webhook_url,
            secret_token=WEBHOOK_SECRET or None,
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=True,
        )
        log.info("🔗 Webhook do Telegram registrado: %s", webhook_url)
    else:
        # ── Modo POLLING (desenvolvimento local sem domínio) ──
        log.info("🔄 BASE_URL não configurada – usando polling local...")
        asyncio.create_task(_start_polling())

    yield   # app rodando

    # ── encerramento limpo ──
    if USE_WEBHOOK and BASE_URL:
        await bot.delete_webhook(drop_pending_updates=True)
        log.info("🔗 Webhook removido do Telegram.")
    await bot.session.close()
    log.info("🤖 Bot encerrado com sucesso.")


async def _start_polling():
    await asyncio.sleep(0.5)
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


# ──────────────────────────────────────────────
# FASTAPI APP
# ──────────────────────────────────────────────
app = FastAPI(title="VIP Bot – Webhook Server", lifespan=lifespan)


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINT 1 – WEBHOOK DO TELEGRAM
# O Telegram manda aqui cada mensagem/botão que o usuário envia para o bot
# ─────────────────────────────────────────────────────────────────────────────
@app.post(TG_WEBHOOK_PATH)
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: Optional[str] = Header(default=None),
):
    # Valida o secret token que configuramos no set_webhook
    if WEBHOOK_SECRET and x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        log.warning("⚠️ Token inválido no webhook do Telegram.")
        raise HTTPException(status_code=403, detail="Token inválido")

    body   = await request.json()
    update = Update.model_validate(body, context={"bot": bot})
    await dp.feed_update(bot, update)
    return JSONResponse({"ok": True})


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINT 2 – WEBHOOK DA VEXOPAY
# A VexoPay manda aqui quando um PIX é pago
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/webhook/vexopay")
async def vexopay_webhook(
    request: Request,
    x_vexopay_signature: Optional[str] = Header(default=None),
):
    body = await request.body()

    # Validação HMAC (use VEXOPAY_SECRET para isso)
    if VEXOPAY_SECRET:
        expected = hmac.new(VEXOPAY_SECRET.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, x_vexopay_signature or ""):
            log.warning("⚠️ Assinatura VexoPay inválida!")
            raise HTTPException(status_code=401, detail="Assinatura inválida")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="JSON inválido")

    log.info("📩 Webhook VexoPay: %s", payload)

    status         = payload.get("status", "").lower()
    transaction_id = payload.get("transaction_id") or payload.get("id", "")
    customer_id    = (payload.get("customer_id")
                      or payload.get("metadata", {}).get("telegram_user_id", ""))

    if status != "paid":
        return JSONResponse({"ok": True, "message": f"status '{status}' ignorado"})

    payment = db_mark_payment_paid(transaction_id)
    if not payment:
        log.info("Pagamento %s não encontrado ou já processado.", transaction_id)
        return JSONResponse({"ok": True, "message": "já processado"})

    telegram_id = int(customer_id or payment["telegram_id"])

    # Entrega o conteúdo em background
    asyncio.create_task(deliver_pack(telegram_id, payment["pack_id"]))

    # Notifica o admin
    pack = db_get_pack(payment["pack_id"])
    asyncio.create_task(bot.send_message(
        ADMIN_ID,
        f"💳 *Venda confirmada!*\n"
        f"👤 Usuário ID: `{telegram_id}`\n"
        f"📦 Pacote: *{pack['name'] if pack else payment['pack_id']}*\n"
        f"💰 Valor: R$ {payment['amount']:.2f}\n"
        f"🔑 TX: `{transaction_id}`",
        parse_mode=ParseMode.MARKDOWN,
    ))

    return JSONResponse({"ok": True})


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINTS UTILITÁRIOS
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    """Health check – use no UptimeRobot para manter o serviço acordado."""
    return {"status": "online", "timestamp": datetime.utcnow().isoformat()}


@app.get("/webhook/info")
async def webhook_info():
    """Mostra o status do webhook configurado no Telegram."""
    info = await bot.get_webhook_info()
    return {
        "url":                    info.url,
        "has_custom_certificate": info.has_custom_certificate,
        "pending_update_count":   info.pending_update_count,
        "last_error_message":     info.last_error_message,
        "last_error_date":        str(info.last_error_date) if info.last_error_date else None,
        "max_connections":        info.max_connections,
    }


@app.delete("/webhook/reset")
async def webhook_reset():
    """Remove e re-registra o webhook. Útil após trocar domínio."""
    await bot.delete_webhook(drop_pending_updates=True)
    if BASE_URL:
        webhook_url = f"{BASE_URL}{TG_WEBHOOK_PATH}"
        await bot.set_webhook(
            url=webhook_url,
            secret_token=WEBHOOK_SECRET or None,
            allowed_updates=["message", "callback_query"],
        )
        return {"ok": True, "webhook": webhook_url}
    return {"ok": True, "message": "webhook removido (sem BASE_URL para re-registrar)"}


# ──────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────
if __name__ == "__main__":
    if not BOT_TOKEN:
        log.critical("❌ BOT_TOKEN não configurado no .env!")
        raise SystemExit(1)
    if not ADMIN_ID:
        log.critical("❌ ADMIN_ID não configurado no .env!")
        raise SystemExit(1)
    if USE_WEBHOOK and not BASE_URL:
        log.warning(
            "⚠️  USE_WEBHOOK=true mas BASE_URL está vazia.\n"
            "   O bot vai usar polling local até BASE_URL ser configurada."
        )

    log.info("🚀 Iniciando VIP Bot na porta %s (webhook=%s)", PORT, USE_WEBHOOK and bool(BASE_URL))
    uvicorn.run(
        "bot:app",
        host="0.0.0.0",
        port=PORT,      # Railway injeta a var PORT automaticamente
        reload=False,
        log_level="info",
        access_log=True,
    )
