"""Команды бота: запуск мини-приложения, пополнение Stars, чеки, админка."""
from __future__ import annotations

import html
import logging
import time
from urllib.parse import quote

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BusinessConnection,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    PreCheckoutQuery,
    WebAppInfo,
)

from .casino import Casino, GameError
from .config import Config
from .web import DEPOSIT_MAX, DEPOSIT_MIN, deposit_invoice_kwargs, parse_deposit_payload
from .nft import deliver as deliver_nft, describe as describe_nft, sync as sync_nfts
from .withdraw import admin_keyboard, approve, reject

log = logging.getLogger(__name__)

RULES = (
    "Пополнить баланс Svag Gifts можно через Telegram Stars или чеком. "
    "<b>Вывод — подарками Telegram</b>: выберите подарок в мини-приложении (раздел «Вывод»), "
    "после проверки администратором он придёт вам в Telegram, его можно оставить или обменять на звёзды. "
    "Звёзды из чеков и бонусов нужно сначала отыграть. Играйте ответственно. 18+."
)


def play_keyboard(cfg: Config) -> InlineKeyboardMarkup | None:
    if not cfg.webapp_url:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎁 Играть в Svag Gifts", web_app=WebAppInfo(url=cfg.webapp_url))
    ]])


def build_router(cfg: Config, casino: Casino) -> Router:
    router = Router(name="casino")

    async def register(message: Message) -> dict:
        u = message.from_user
        return await casino.register(u.id, u.username, u.first_name)

    # ---------- игроки ----------

    @router.message(CommandStart())
    async def start(message: Message, command: CommandObject) -> None:
        user = await register(message)
        arg = (command.args or "").strip()
        if arg.startswith("c_"):
            try:
                amount, balance = await casino.activate_check(user["id"], arg[2:])
                await message.answer(f"🎁 Чек активирован: <b>+{amount} ⭐</b>\nБаланс: {balance} ⭐")
            except GameError as e:
                await message.answer(f"⚠️ {html.escape(str(e))}")
            user = await casino.db.get_user(user["id"])
        await message.answer(
            f"🎁 <b>Добро пожаловать в Svag Gifts!</b>\n\n"
            f"Слоты, краш, мины, кости, кейсы, PvP-рулетка и PvP-хоккей — в мини-приложении.\n"
            f"Вывод — подарками Telegram.\n"
            f"Баланс: <b>{user['balance']} ⭐</b>\n\n"
            f"/deposit — пополнить звёздами\n/balance — баланс\n/help — правила",
            reply_markup=play_keyboard(cfg),
        )

    @router.message(Command("help", "rules"))
    async def rules(message: Message) -> None:
        await register(message)
        text = RULES + "\n\n/deposit — пополнить\n/balance — баланс\n/paysupport — вопросы по оплате"
        if cfg.is_admin(message.from_user.id):
            text += "\n\n/admin — команды администратора"
        await message.answer(text, reply_markup=play_keyboard(cfg))

    @router.message(Command("balance"))
    async def balance(message: Message) -> None:
        user = await register(message)
        await message.answer(f"Баланс: <b>{user['balance']} ⭐</b>", reply_markup=play_keyboard(cfg))

    @router.message(Command("deposit"))
    async def deposit(message: Message, command: CommandObject) -> None:
        await register(message)
        arg = (command.args or "").strip()
        if not arg.isdigit() or not DEPOSIT_MIN <= int(arg) <= DEPOSIT_MAX:
            await message.answer(
                f"Напишите сумму от {DEPOSIT_MIN} до {DEPOSIT_MAX}, например: <code>/deposit 100</code>\n"
                "Или пополните баланс в мини-приложении."
            )
            return
        await message.answer_invoice(**deposit_invoice_kwargs(message.from_user.id, int(arg)))

    @router.message(Command("paysupport"))
    async def paysupport(message: Message) -> None:
        await message.answer(
            "По вопросам оплаты напишите администратору. Пополнения зачисляются на игровой баланс "
            "автоматически; если звёзды списались, а баланс не пополнился, пришлите время платежа."
        )

    # ---------- оплата Stars ----------

    @router.pre_checkout_query()
    async def pre_checkout(query: PreCheckoutQuery) -> None:
        parsed = parse_deposit_payload(query.invoice_payload)
        ok = (
            parsed is not None
            and query.currency == "XTR"
            and parsed[0] == query.from_user.id
            and parsed[1] == query.total_amount
        )
        if ok:
            await query.answer(ok=True)
        else:
            await query.answer(ok=False, error_message="Счёт недействителен, создайте новый.")

    @router.message(F.successful_payment)
    async def paid(message: Message) -> None:
        pay = message.successful_payment
        if pay.currency != "XTR":
            return
        credited = await casino.db.credit_payment(
            pay.telegram_payment_charge_id, message.from_user.id, pay.total_amount
        )
        user = await casino.db.get_user(message.from_user.id)
        if credited:
            await message.answer(
                f"✅ Зачислено <b>{pay.total_amount} ⭐</b>\nБаланс: {user['balance']} ⭐",
                reply_markup=play_keyboard(cfg),
            )

    # ---------- администратор ----------

    admin = Router(name="admin")
    admin.message.filter(F.from_user.id.in_(cfg.admin_ids))
    admin.callback_query.filter(F.from_user.id.in_(cfg.admin_ids))

    @admin.message(Command("admin"))
    async def admin_help(message: Message) -> None:
        await message.answer(
            "<b>Команды администратора</b>\n"
            "/check <code>сумма [активаций]</code> — создать чек, например <code>/check 100 5</code>\n"
            "/withdrawals — заявки на вывод\n"
            "/nfts — NFT на вашем аккаунте для NFT-кейса\n"
            "/nftprice <code>номер цена</code> — назначить цену NFT (после проверки на маркете)\n"
            "/nftoff <code>номер</code> — убрать NFT из кейса\n"
            "/nftsend <code>номер</code> — повторить передачу выигранного NFT\n"
            "/stars — баланс звёзд бота (из него отправляются подарки)\n"
            "/checks — активные чеки\n"
            "/revoke <code>код</code> — отозвать чек\n"
            "/stats — статистика казино\n"
            "/user <code>@username или ID</code> — баланс игрока"
        )

    @admin.message(Command("check"))
    async def make_check(message: Message, command: CommandObject, bot: Bot) -> None:
        args = (command.args or "").split()
        if not args or not all(a.isdigit() for a in args[:2]):
            await message.answer("Формат: <code>/check сумма [активаций]</code>, например <code>/check 100 5</code>")
            return
        amount = int(args[0])
        activations = int(args[1]) if len(args) > 1 else 1
        try:
            code = await casino.create_check(message.from_user.id, amount, activations)
        except GameError as e:
            await message.answer(f"⚠️ {html.escape(str(e))}")
            return
        me = await bot.me()
        link = f"https://t.me/{me.username}?start=c_{code}"
        log.info("Админ %s создал чек на %s ⭐ × %s", message.from_user.id, amount, activations)
        await message.answer(
            f"🎁 <b>Чек на {amount} ⭐</b>\nАктиваций: {activations}\nКод: <code>{code}</code>\n\n{link}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"🎁 Получить {amount} ⭐", url=link)],
                [InlineKeyboardButton(
                    text="📤 Поделиться",
                    url=f"https://t.me/share/url?url={quote(link, safe='')}&text={quote(f'Чек на {amount} ⭐')}",
                )],
            ]),
        )

    @admin.message(Command("checks"))
    async def list_checks(message: Message) -> None:
        checks = await casino.list_checks()
        if not checks:
            await message.answer("Активных чеков нет.")
            return
        lines = [f"<code>{c['code']}</code> — {c['amount']} ⭐, осталось {c['left']}/{c['total']}" for c in checks]
        await message.answer("<b>Активные чеки</b>\n" + "\n".join(lines))

    @admin.message(Command("revoke"))
    async def revoke(message: Message, command: CommandObject) -> None:
        code = (command.args or "").strip().removeprefix("c_")
        if not code:
            await message.answer("Формат: <code>/revoke код</code>")
            return
        ok = await casino.revoke_check(code)
        await message.answer("🗑 Чек отозван." if ok else "Чек не найден или уже отозван.")

    @admin.message(Command("stats"))
    async def stats(message: Message) -> None:
        s = await casino.db.stats()
        profit = s["wagered"] - s["won"]
        await message.answer(
            "📊 <b>Статистика</b>\n"
            f"Игроков: {s['users']}\n"
            f"Пополнений: {s['payments']} на {s['deposited']} ⭐\n"
            f"Ставок: {s['bets']} на {s['wagered']} ⭐, выплачено {s['won']} ⭐\n"
            f"Доход казино с игр: {profit} ⭐\n"
            f"Выдано по чекам: {s['checks_redeemed']} ⭐, ещё можно активировать на {s['checks_liability']} ⭐ "
            f"({s['active_checks']} чеков)\n"
            f"Звёзд на балансах игроков: {s['balances']} ⭐\n"
            f"Выведено подарками: {s['withdrawn']} ⭐, ждут решения: {s['withdraw_pending']} ⭐"
        )

    @admin.message(Command("withdrawals"))
    async def list_withdrawals(message: Message) -> None:
        pending = await casino.withdrawals(status="pending", limit=20)
        if not pending:
            await message.answer("Заявок на вывод нет.")
            return
        for wd in pending:
            user = await casino.db.get_user(wd["user_id"])
            name = html.escape((user or {}).get("first_name") or str(wd["user_id"]))
            err = f"\n⚠️ Прошлая попытка: {html.escape(wd['error'])}" if wd["error"] else ""
            await message.answer(
                f"💸 №{wd['id']}: {name} (<code>{wd['user_id']}</code>) — "
                f"{wd['gift_emoji'] or '🎁'} {wd['amount']} ⭐{err}",
                reply_markup=admin_keyboard(wd["id"]),
            )

    @admin.message(Command("stars"))
    async def bot_stars(message: Message, bot: Bot) -> None:
        try:
            balance = await bot.get_my_star_balance()
            await message.answer(f"⭐ Баланс звёзд бота: <b>{balance.amount}</b>\nИз него отправляются подарки при выводе.")
        except Exception as e:
            await message.answer(f"Не удалось получить баланс: {html.escape(str(e))}")

    @router.business_connection()
    async def business_connection(conn: BusinessConnection) -> None:
        """Админ подключает бота к своему аккаунту, чтобы отдавать NFT из кейсов."""
        rights = conn.rights
        can_gifts = bool(rights and rights.can_view_gifts_and_stars and rights.can_transfer_and_upgrade_gifts)
        async with casino.db.tx() as c:
            await c.execute(
                "INSERT INTO business_connections(id, user_id, can_gifts, is_enabled, updated_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET user_id=excluded.user_id, can_gifts=excluded.can_gifts, "
                "is_enabled=excluded.is_enabled, updated_at=excluded.updated_at",
                (conn.id, conn.user.id, int(can_gifts), int(conn.is_enabled), time.time()),
            )
        if not cfg.is_admin(conn.user.id):
            return
        if not conn.is_enabled:
            text = "🔌 Бот отключён от вашего аккаунта — NFT-кейс выключен."
        elif not can_gifts:
            text = ("⚠️ Бот подключён, но без прав на подарки. В настройках чат-бота включите "
                    "просмотр подарков и звёзд и передачу подарков.")
        else:
            text = "🔌 Бот подключён к аккаунту с правами на подарки. Отправьте /nfts, чтобы увидеть NFT."
        try:
            await conn.bot.send_message(conn.user.id, text)
        except Exception:
            pass

    @admin.message(Command("nfts"))
    async def nfts(message: Message, bot: Bot) -> None:
        count, error = await sync_nfts(bot, casino.db, cfg)
        if error:
            await message.answer(f"⚠️ {html.escape(error)}")
            return
        rows = await casino.db.all(
            "SELECT * FROM nft_prizes WHERE status IN ('available','won','sending','failed') ORDER BY id"
        )
        if not rows:
            await message.answer("На аккаунте нет NFT-подарков, которые можно передать.")
            return
        status_text = {"available": "", "won": " — выигран, передаётся", "sending": " — передаётся",
                       "failed": " — ⚠️ не передан, /nftsend"}
        lines = [f"<b>{r['id']}.</b> {describe_nft(r)}{status_text.get(r['status'], '')}" for r in rows]
        priced = sum(1 for r in rows if r["status"] == "available" and r["price"])
        await message.answer(
            f"💎 <b>NFT на аккаунте: {count}</b>, в кейсе: {priced}\n\n" + "\n".join(lines) +
            f"\n\nЦена NFT-кейса: {cfg.nft_case_price} ⭐. В кейс попадают только NFT с ценой. "
            "Бот не видит рыночную цену через API — проверьте цену на маркете Telegram (профиль → подарок → "
            "«Купить похожие» / маркетплейс) и назначьте: <code>/nftprice номер цена</code>."
        )

    @admin.message(Command("nftprice"))
    async def nft_price(message: Message, command: CommandObject) -> None:
        args = (command.args or "").split()
        if len(args) != 2 or not all(a.isdigit() for a in args) or int(args[1]) < 1:
            await message.answer("Формат: <code>/nftprice номер цена</code>, например <code>/nftprice 3 5000</code>")
            return
        async with casino.db.tx() as c:
            cur = await c.execute(
                "UPDATE nft_prizes SET price=?, updated_at=? WHERE id=? AND status='available'",
                (int(args[1]), time.time(), int(args[0])),
            )
        if cur.rowcount != 1:
            await message.answer("NFT не найден или уже выигран. Список — /nfts")
            return
        n = await casino.db.one("SELECT * FROM nft_prizes WHERE id=?", int(args[0]))
        await message.answer(f"✅ {describe_nft(n)}\nNFT в кейсе.")

    @admin.message(Command("nftoff"))
    async def nft_off(message: Message, command: CommandObject) -> None:
        arg = (command.args or "").strip()
        if not arg.isdigit():
            await message.answer("Формат: <code>/nftoff номер</code>")
            return
        async with casino.db.tx() as c:
            cur = await c.execute(
                "UPDATE nft_prizes SET price=NULL WHERE id=? AND status='available'", (int(arg),)
            )
        await message.answer("NFT убран из кейса." if cur.rowcount == 1 else "NFT не найден.")

    @admin.message(Command("nftsend"))
    async def nft_send(message: Message, command: CommandObject, bot: Bot) -> None:
        arg = (command.args or "").strip()
        if not arg.isdigit():
            await message.answer("Формат: <code>/nftsend номер</code>")
            return
        ok, text = await deliver_nft(bot, casino.db, cfg, int(arg))
        await message.answer(("✅ " if ok else "⚠️ ") + html.escape(text))

    @admin.callback_query(F.data.startswith("w:"))
    async def withdraw_decision(query: CallbackQuery, bot: Bot) -> None:
        _, action, raw_id = query.data.split(":", 2)
        wd_id = int(raw_id)
        if action == "ok":
            ok, text = await approve(bot, casino, wd_id, query.from_user.id)
        else:
            ok, text = await reject(bot, casino, wd_id, query.from_user.id)
        await query.answer(text[:190], show_alert=not ok)
        if isinstance(query.message, Message):
            try:
                if ok:
                    await query.message.edit_text(f"{query.message.html_text}\n\n<b>{html.escape(text)}</b>")
                else:
                    await query.message.answer(html.escape(text))
            except Exception:
                pass

    @admin.message(Command("user"))
    async def user_info(message: Message, command: CommandObject) -> None:
        user = await casino.db.find_user(command.args or "")
        if not user:
            await message.answer("Игрок не найден. Формат: <code>/user @username</code> или <code>/user ID</code>")
            return
        await message.answer(
            f"<b>{html.escape(user['first_name'] or '')}</b> @{html.escape(user['username'] or '—')} "
            f"(<code>{user['id']}</code>)\n"
            f"Баланс: {user['balance']} ⭐\nПополнил: {user['deposited']} ⭐\n"
            f"Поставил: {user['wagered']} ⭐, выиграл: {user['won']} ⭐"
        )

    root = Router(name="root")
    root.include_router(admin)
    root.include_router(router)
    return root
