"""Команды бота: запуск мини-приложения, пополнение Stars, чеки, админка."""
from __future__ import annotations

import asyncio
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
from .web import (DEPOSIT_MAX, DEPOSIT_MIN, deposit_invoice_kwargs, notify_referrer, parse_deposit_payload,
                  parse_referral, referral_link)
from .db import REFERRAL_RATE
from .mrkt import STAR_USD
from .channel import CHANNEL
from .nft import deliver as deliver_nft, describe as describe_nft, sync as sync_nfts
from .relayer import Relayer
from . import broadcast as bc, money, ton
from .withdraw import admin_keyboard, approve, reject, ton_admin_keyboard, ton_decide

log = logging.getLogger(__name__)

SLOT_TEXT = {"bar": "BAR", "grape": "🍇", "lemon": "🍋", "seven": "7️⃣"}

RULES = (
    "Играть можно на звёзды или на TON (переключатель ★/💎 в мини-приложении). "
    "Пополнить баланс Triple Gifts можно через Telegram Stars, TON-переводом, чеком или подарком: отправьте его аккаунту казино "
    "(мини-приложение → Кошелёк → Подарки), NFT можно ставить в PvP. "
    "<b>Вывод — подарками Telegram</b>: выберите подарок в мини-приложении (раздел «Вывод»), "
    "после проверки администратором он придёт вам в Telegram, его можно оставить или обменять на звёзды. "
    "Звёзды из чеков и бонусов нужно сначала отыграть. Играйте ответственно. 18+."
)


def play_keyboard(cfg: Config) -> InlineKeyboardMarkup | None:
    if not cfg.webapp_url:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎰 Открыть Triple Gifts", web_app=WebAppInfo(url=cfg.webapp_url))],
        [InlineKeyboardButton(text="📢 Наш канал", url=f"https://t.me/{CHANNEL.lstrip('@')}")],
    ])


def build_router(cfg: Config, casino: Casino, relayer: Relayer | None = None, on_relayer_ready=None) -> Router:
    router = Router(name="casino")

    async def register(message: Message) -> dict:
        u = message.from_user
        return await casino.register(u.id, u.username, u.first_name)

    # ---------- игроки ----------

    @router.message(CommandStart())
    async def start(message: Message, command: CommandObject) -> None:
        user = await register(message)
        arg = (command.args or "").strip()
        ref = parse_referral(arg)
        if ref and await casino.db.set_referrer(user["id"], ref):
            await notify_referrer(message.bot, ref, message.from_user.first_name)
        if arg.startswith("c_"):
            try:
                amount, balance = await casino.activate_check(user["id"], arg[2:])
                await message.answer(f"🎁 Чек активирован: <b>+{amount} ⭐</b>\nБаланс: {balance} ⭐")
            except GameError as e:
                await message.answer(f"⚠️ {html.escape(str(e))}")
            user = await casino.db.get_user(user["id"])
        await message.answer(
            "🎰 <b>Triple Gifts</b> — казино на подарках Telegram.\n\n"
            "Все игры, кошелёк, кейсы и NFT — в мини-приложении. Жмите кнопку ниже 👇",
            reply_markup=play_keyboard(cfg),
        )

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
        if credited and user.get("referrer_id") and int(pay.total_amount * REFERRAL_RATE) > 0:
            try:
                await message.bot.send_message(
                    user["referrer_id"],
                    f"🤝 Ваш реферал пополнил баланс — вам <b>+{int(pay.total_amount * REFERRAL_RATE)} ⭐</b>",
                )
            except Exception:
                pass
        if credited:
            await message.answer(
                f"✅ Зачислено <b>{pay.total_amount} ⭐</b>\nБаланс: {user['balance']} ⭐",
                reply_markup=play_keyboard(cfg),
            )

    @router.message(F.chat.type == "private")
    async def anything(message: Message) -> None:
        """Всё, кроме /start и оплаты, — только в мини-приложении."""
        await register(message)
        await message.answer("🎰 Играть, пополнять и выводить — в мини-приложении Triple Gifts 👇",
                             reply_markup=play_keyboard(cfg))

    # ---------- администратор ----------

    admin = Router(name="admin")
    admin.message.filter(F.from_user.id.in_(cfg.admin_ids))
    admin.callback_query.filter(F.from_user.id.in_(cfg.admin_ids))

    # ---------- чеки админа (игроки создают чеки в мини-приложении) ----------

    @admin.message(Command("check"))
    async def make_check(message: Message, command: CommandObject, bot: Bot) -> None:
        await register(message)
        is_admin = message.from_user.id in cfg.admin_ids
        args = (command.args or "").split()
        if not args or not all(a.isdigit() for a in args[:2]):
            await message.answer(
                "🎁 <b>Чеки</b>\nФормат: <code>/check сумма [активаций]</code>, например <code>/check 100 5</code> — "
                "5 человек получат по 100 ⭐."
                + ("" if is_admin else "\nСумма × активации сразу списывается с вашего баланса. "
                   "Неактивированное можно вернуть: /mychecks, /revoke код."))
            return
        amount = int(args[0])
        activations = int(args[1]) if len(args) > 1 else 1
        try:
            code = await casino.create_check(message.from_user.id, amount, activations, paid=not is_admin)
        except GameError as e:
            await message.answer(f"⚠️ {html.escape(str(e))}")
            return
        me = await bot.me()
        link = f"https://t.me/{me.username}?start=c_{code}"
        log.info("%s %s создал чек на %s ⭐ × %s", "Админ" if is_admin else "Игрок", message.from_user.id,
                 amount, activations)
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

    @admin.message(Command("mychecks"))
    async def my_checks(message: Message) -> None:
        checks = await casino.my_checks(message.from_user.id)
        if not checks:
            await message.answer("У вас нет активных чеков. Создать: <code>/check 100 5</code>")
            return
        lines = [f"<code>{c['code']}</code> — {c['amount']} ⭐, осталось {c['left']}/{c['total']}" for c in checks]
        await message.answer("<b>Ваши чеки</b>\n" + "\n".join(lines) + "\n\nОтозвать и вернуть остаток: /revoke код")

    @admin.message(Command("revoke"))
    async def revoke(message: Message, command: CommandObject) -> None:
        code = (command.args or "").strip().removeprefix("c_")
        if not code:
            await message.answer("Формат: <code>/revoke код</code>")
            return
        is_admin = message.from_user.id in cfg.admin_ids
        refund = await casino.revoke_check(code, None if is_admin else message.from_user.id)
        if refund is None:
            await message.answer("Чек не найден или уже отозван.")
        else:
            await message.answer("🗑 Чек отозван." + (f" Возвращено {refund} ⭐ создателю чека." if refund else ""))


    @admin.message(Command("admin"))
    async def admin_help(message: Message) -> None:
        await message.answer(
            "<b>Команды администратора</b>\n"
            "/check <code>сумма [активаций]</code> — создать чек, например <code>/check 100 5</code>\n"
            "/withdrawals — заявки на вывод\n"
            "/relayer — релейер NFT: подключение и вход\n"
            "/nfts — модели NFT у релейера и их цены с маркета\n"
            "/nftoff, /nfton <code>номер</code> — убрать/вернуть модель в NFT-кейс\n"
            "/nftsend <code>номер выигрыша</code> — повторить передачу NFT\n"
            "/tonrate — курс TON → звёзды для цен MRKT\n"
            "/dupe <code>N</code> — демо-NFT (настоящие модели с MRKT, видны всем), /dupe_clear — удалить\n"
            "/tonwallet <code>адрес</code> — кошелёк казино для пополнений TON\n"
            "/stars — звёзды релейера (из них отправляются подарки при выводе)\n"
            "/checks — активные чеки\n"
            "/revoke <code>код</code> — отозвать чек\n"
            "/stats — статистика казино\n"
            "/rebrand <code>[юзернейм]</code> — имя бота и оформление релейера (Triple Gifts)\n"
            "/channel_setup — оформить канал и опубликовать приветственный пост\n"
            "/channel_post — ещё раз опубликовать приветственный пост\n"
            "/channel_wins <code>on|off</code> — выигрыши в канал\n"
            "/league_icons — значки лиг в футболе (из премиум-эмодзи)\n"
            "/sports_refresh — обновить матчи футбола сейчас\n"
            "/user <code>@username или ID</code> — баланс игрока\n"
            "/broadcast — рассылка игрокам (всем, пополнявшим, активным… или своему списку)\n"
            "/take <code>@user|ID сумма|all [ton] [причина]</code> — тихо списать баланс (абуз уязвимостей)\n"
            "/give <code>@user|ID сумма [ton] [причина]</code> — тихо вернуть/начислить"
        )

    @admin.message(Command("checks"))
    async def list_checks(message: Message) -> None:
        checks = await casino.list_checks()
        if not checks:
            await message.answer("Активных чеков нет.")
            return
        lines = [f"<code>{c['code']}</code> — {c['amount']} ⭐, осталось {c['left']}/{c['total']}" for c in checks]
        await message.answer("<b>Активные чеки</b>\n" + "\n".join(lines))

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
            f"Выведено подарками: {s['withdrawn']} ⭐, ждут решения: {s['withdraw_pending']} ⭐\n\n"
            "💎 <b>TON</b>\n"
            f"Пополнений: {s['ton_payments']} на {money.fmt(s['ton_deposited'], money.TON)}\n"
            f"Ставки: {money.fmt(s['ton_wagered'], money.TON)}, выплачено {money.fmt(s['ton_won'], money.TON)}, "
            f"доход {money.fmt(s['ton_wagered'] - s['ton_won'], money.TON)}\n"
            f"TON на балансах игроков: {money.fmt(s['ton_balances'], money.TON)}\n"
            f"Выведено: {money.fmt(s['ton_withdrawn'], money.TON)}, ждут: "
            f"{money.fmt(s['ton_withdraw_pending'], money.TON)}"
        )

    @admin.message(Command("withdrawals"))
    async def list_withdrawals(message: Message) -> None:
        pending = await casino.withdrawals(status="pending", limit=20)
        ton_pending = await casino.ton_withdrawals(status="pending", limit=20)
        if not pending and not ton_pending:
            await message.answer("Заявок на вывод нет.")
            return
        for wd in ton_pending:
            user = await casino.db.get_user(wd["user_id"])
            name = html.escape((user or {}).get("first_name") or str(wd["user_id"]))
            await message.answer(
                f"💎 TON №{wd['id']}: {name} (<code>{wd['user_id']}</code>) — "
                f"<b>{money.fmt(wd['amount'], money.TON)}</b> на <code>{html.escape(wd['address'])}</code>",
                reply_markup=ton_admin_keyboard(wd["id"]),
            )
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
        lines = []
        try:
            stars = await relayer.stars_balance() if relayer is not None else None
            lines.append(f"⭐ Звёзды релейера: <b>{stars}</b> — из них отправляются подарки при выводе "
                         "и оплачиваются платные передачи NFT." if stars is not None
                         else "⚠️ Релейер не подключён — выводы не отправятся. Войдите: /relayer")
        except Exception as e:
            lines.append(f"Не удалось получить баланс релейера: {html.escape(str(e))}")
        try:
            balance = await bot.get_my_star_balance()
            lines.append(f"🤖 Звёзды бота (пополнения игроков): <b>{balance.amount}</b>")
        except Exception as e:
            lines.append(f"Не удалось получить баланс бота: {html.escape(str(e))}")
        await message.answer("\n".join(lines))

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
            text = "🔌 Релейер отключён от бота — NFT-кейс выключен."
        elif not can_gifts:
            text = ("⚠️ Бот подключён, но без прав на подарки. В настройках чат-бота включите "
                    "просмотр подарков и звёзд и передачу подарков.")
        else:
            text = "🔌 Релейер подключён с правами на подарки. Отправьте /nfts, чтобы увидеть модели и цены."
        try:
            await conn.bot.send_message(conn.user.id, text)
        except Exception:
            pass

    async def forget(message: Message) -> None:
        """Удаляет из чата сообщение с кодом/паролем/ключом."""
        try:
            await message.delete()
        except Exception:
            pass

    @admin.message(Command("relayer"))
    async def relayer_status(message: Message) -> None:
        who = await relayer.me() if relayer and relayer.ready else None
        lines = [
            "🤖 <b>Релейер NFT</b> — аккаунт казино, на котором лежат NFT-подарки. Через него бот узнаёт цены "
            "моделей на маркете Telegram и передаёт выигранные NFT игрокам.",
            "",
            f"Вход: {'✅ ' + html.escape(who) if who else '❌ не выполнен'}",
            "1. Получите api_id и api_hash на my.telegram.org → API development tools.",
            "2. <code>/relayer_api api_id api_hash</code>",
            "3. <code>/relayer_phone +79990000000</code> — номер аккаунта-релейера.",
            "4. Код из Telegram — <b>с пробелами</b>: <code>/relayer_code 1 2 3 4 5</code> (слитный Telegram блокирует).",
            "5. Если есть облачный пароль: <code>/relayer_password пароль</code>.",
            "6. <code>/nfts</code> — модели у релейера и их пол на маркете.",
            "",
            "Сообщения с ключами, кодом и паролем бот сразу удаляет, сессия хранится зашифрованной.",
            "Платную передачу NFT релейер оплачивает звёздами со своего баланса — держите на нём немного звёзд.",
        ]
        await message.answer("\n".join(lines))

    @admin.message(Command("relayer_api"))
    async def relayer_api(message: Message, command: CommandObject) -> None:
        await forget(message)
        args = (command.args or "").split()
        if len(args) != 2 or not args[0].isdigit():
            await message.answer("Формат: <code>/relayer_api api_id api_hash</code>")
            return
        await relayer.set_api(int(args[0]), args[1])
        await message.answer("✅ api_id и api_hash сохранены. Теперь: <code>/relayer_phone +79990000000</code>")

    @admin.message(Command("relayer_phone"))
    async def relayer_phone(message: Message, command: CommandObject) -> None:
        phone = (command.args or "").replace(" ", "")
        if not phone.lstrip("+").isdigit():
            await message.answer("Формат: <code>/relayer_phone +79990000000</code>")
            return
        try:
            await relayer.send_code(phone)
        except Exception as e:
            await message.answer(f"⚠️ {html.escape(str(e))}")
            return
        await message.answer("📨 Код отправлен в Telegram релейера. Пришлите его <b>с пробелами</b>: "
                             "<code>/relayer_code 1 2 3 4 5</code>")

    @admin.message(Command("relayer_code"))
    async def relayer_code(message: Message, command: CommandObject) -> None:
        await forget(message)
        code = "".join(ch for ch in (command.args or "") if ch.isdigit())
        if not code:
            await message.answer("Формат: <code>/relayer_code 1 2 3 4 5</code>")
            return
        try:
            done = await relayer.sign_in(code)
        except Exception as e:
            await message.answer(f"⚠️ {html.escape(str(e))}")
            return
        if not done:
            await message.answer("🔐 Включён облачный пароль: <code>/relayer_password пароль</code>")
            return
        if on_relayer_ready:
            on_relayer_ready()
        await message.answer(f"✅ Релейер подключён: {html.escape(await relayer.me() or '')}. Теперь /nfts")

    @admin.message(Command("relayer_password"))
    async def relayer_password(message: Message, command: CommandObject) -> None:
        await forget(message)
        try:
            await relayer.sign_in_password((command.args or "").strip())
        except Exception as e:
            await message.answer(f"⚠️ {html.escape(str(e))}")
            return
        if on_relayer_ready:
            on_relayer_ready()
        await message.answer(f"✅ Релейер подключён: {html.escape(await relayer.me() or '')}. Теперь /nfts")

    @admin.message(Command("relayer_logout"))
    async def relayer_logout(message: Message) -> None:
        await relayer.logout()
        await message.answer("Релейер отключён, сессия удалена.")

    @admin.message(Command("nfts"))
    async def nfts(message: Message, bot: Bot) -> None:
        status = await message.answer("🔄 Обновляю подарки релейера и цены с маркета…")
        try:
            count, error = await sync_nfts(casino.db, relayer)
        except Exception as e:
            log.exception("Не удалось обновить NFT-модели")
            await status.edit_text(f"⚠️ Не удалось обновить: {html.escape(type(e).__name__)}: "
                                   f"{html.escape(str(e)[:300])}")
            return
        rows = await casino.db.all("SELECT * FROM nft_models WHERE (stock > 0 OR reserved > 0) "
                                   "ORDER BY price DESC")
        if not rows:
            text = f"⚠️ {html.escape(error)}" if error else "У релейера нет NFT-подарков, которые можно передать."
            await status.edit_text(text)
            return
        lines = [f"<b>{r['id']}.</b> {describe_nft(r)}" for r in rows]
        note = f"\n\n⚠️ {html.escape(error)}" if error else ""
        await status.edit_text(
            f"💎 <b>Модели у релейера: {count}</b>\n\n" + "\n".join(lines) +
            f"\n\nNFT-кейс: {cfg.nft_case_price} ⭐. В кейс попадают модели с проверенной ценой и свободным запасом; "
            "выигравшему релейер передаёт случайный подарок этой модели." + note
        )

    async def toggle_model(message: Message, command: CommandObject, enabled: bool) -> None:
        arg = (command.args or "").strip()
        if not arg.isdigit():
            await message.answer("Формат: номер модели из /nfts")
            return
        async with casino.db.tx() as c:
            cur = await c.execute("UPDATE nft_models SET enabled=? WHERE id=?", (int(enabled), int(arg)))
        await message.answer(("Модель в кейсе." if enabled else "Модель убрана из кейса.") if cur.rowcount else
                             "Модель не найдена.")

    @admin.message(Command("nftoff"))
    async def nft_off(message: Message, command: CommandObject) -> None:
        await toggle_model(message, command, False)

    @admin.message(Command("nfton"))
    async def nft_on(message: Message, command: CommandObject) -> None:
        await toggle_model(message, command, True)

    @admin.message(Command("dupe"))
    async def dupe(message: Message, command: CommandObject) -> None:
        if relayer is None:
            await message.answer("Релейер не настроен — модели берутся с MRKT через него (/relayer)")
            return
        arg = (command.args or "").strip()
        count = min(30, max(3, int(arg))) if arg.isdigit() else 12
        status = await message.answer(f"🔄 Беру {count} настоящих моделей и их флор с MRKT…")
        try:
            mine, models = await casino.demo_add(message.from_user.id, await relayer.market.sample_models(count))
        except Exception as e:
            log.warning("Демо-NFT не созданы", exc_info=True)
            await status.edit_text(f"⚠️ Не получилось: {html.escape(str(e) or type(e).__name__)}")
            return

        def line(m: dict) -> str:
            return f"{m['emoji']} {html.escape(m['title'])} «{html.escape(m['model'])}» — {m['price']} ⭐"

        await status.edit_text(
            f"🧩 <b>Демо-NFT: {len(models)} моделей</b> (цена — флор MRKT, обновляется раз в 10 минут)\n"
            + "\n".join(line(m) for m in models)
            + "\n\nИх видят все: в NFT-кейсах, на 777 и в апгрейде — с пометкой «демо». Подарков у релейера нет, "
              "поэтому выигравший игрок сразу получает флор звёздами.\n"
              f"Вам в «Мои подарки» — {len(mine)} дюпа (их нельзя вывести, можно продать казино и ставить).\n"
              "Ещё модели: <code>/dupe 20</code>, удалить все: /dupe_clear",
        )

    @admin.message(Command("dupe_clear"))
    async def dupe_clear(message: Message) -> None:
        count = await casino.demo_clear()
        await message.answer(f"🧹 Удалено демо-моделей: {count}, дюпы из «Моих подарков» тоже убраны.")

    @admin.message(Command("leaders_reset"))
    async def leaders_reset(message: Message) -> None:
        await casino.leaders_reset()
        await message.answer("🏆 Таблица лидеров очищена — считаем ставки с этого момента.")

    @admin.message(Command("rekey"))
    async def rekey(message: Message, command: CommandObject) -> None:
        """Подготовка к переезду на нового бота: релейер перешифровывается под новый токен (без повторного входа)."""
        import re
        token = (command.args or "").strip()
        try:
            await message.delete()                 # токен не должен висеть в чате
        except Exception:
            pass
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{30,}", token):
            await message.answer("Формат: <code>/rekey токен_нового_бота</code>")
            return
        probe = Bot(token)
        try:
            new_me = await probe.get_me()
        except Exception:
            await message.answer("⚠️ Telegram не принял этот токен — проверьте его в @BotFather")
            return
        finally:
            await probe.session.close()
        if relayer is None:
            await message.answer("Релейер не настроен — переносить нечего.")
            return
        count = await relayer.stage_rekey(token)
        await message.answer(
            f"✅ Релейер подготовлен для @{new_me.username} ({count} из 3 настроек).\n"
            "Теперь замените секрет <b>BOT_TOKEN</b> в GitHub на новый токен и перезапустите хостинг — "
            "новый бот подхватит релейер без повторного входа. Сообщение с токеном я удалил.")

    @admin.message(Command("relayer_restore"))
    async def relayer_restore(message: Message, command: CommandObject) -> None:
        """Бота сменили без /rekey: вернуть релейер, расшифровав его настройки токеном старого бота."""
        import re
        token = (command.args or "").strip()
        try:
            await message.delete()
        except Exception:
            pass
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{30,}", token):
            await message.answer("Формат: <code>/relayer_restore токен_старого_бота</code> (есть в @BotFather)")
            return
        if relayer is None:
            await message.answer("Релейер не настроен.")
            return
        count = await relayer.restore_from_token(token)
        if count < 3:
            await message.answer(f"⚠️ Этим токеном расшифровано {count} из 3 настроек — это не токен старого бота? "
                                 "Можно войти заново: /relayer")
            return
        if await relayer.start():
            if on_relayer_ready:
                on_relayer_ready()
            await message.answer(f"✅ Релейер снова подключён: {html.escape(await relayer.me() or '')}\n"
                                 "Сообщение с токеном я удалил.")
        else:
            await message.answer("⚠️ Настройки восстановлены, но сессия релейера недействительна — войдите заново: /relayer")

    @admin.message(Command("rebrand"))
    async def rebrand(message: Message, command: CommandObject, bot: Bot) -> None:
        """Переименование в Triple Gifts: бот (имя) и аккаунт-релейер (имя, описание, аватарка, юзернейм)."""
        from pathlib import Path
        from .channel import CHANNEL
        lines = ["🎰 <b>Triple Gifts</b>"]
        try:
            await bot.set_my_name("Triple Gifts")
            lines.append("✅ имя бота")
        except Exception as e:
            lines.append(f"⚠️ имя бота: {html.escape(str(getattr(e, 'message', e)))[:100]}")
        if relayer is None or not relayer.ready:
            lines.append("⚠️ релейер не подключён — войдите: /relayer")
        else:
            avatar = (Path(__file__).resolve().parent.parent / "webapp" / "avatar.png").read_bytes()
            try:
                lines += await relayer.rebrand("Triple Gifts", f"Касса казино Triple Gifts · {CHANNEL}", avatar,
                                               (command.args or "").strip() or None)
            except Exception as e:
                lines.append(f"⚠️ релейер: {html.escape(str(e))}")
        lines.append("\nАватарку и юзернейм самого бота меняют в @BotFather (/setuserpic, /setname).")
        await message.answer("\n".join(lines))

    @admin.message(Command("channel_setup"))
    async def channel_setup(message: Message, bot: Bot) -> None:
        from . import channel
        await message.answer(f"Оформляю канал {channel.CHANNEL}…")
        report = await channel.setup(bot)
        tip = ("\n\nЕсли что-то не вышло — добавьте бота админом канала с правами: публикация сообщений, "
               "изменение профиля канала, закрепление." if any(r.startswith("⚠️") for r in report) else "")
        await message.answer("📢 <b>Канал</b>\n" + "\n".join(report) + tip)

    @admin.message(Command("channel_post"))
    async def channel_post(message: Message, bot: Bot) -> None:
        from aiogram.types import BufferedInputFile
        from . import channel
        me = await bot.get_me()
        try:
            await bot.send_photo(channel.CHANNEL, BufferedInputFile(channel.banner(), "triple_gifts.png"),
                                 caption=channel.welcome_text(), reply_markup=channel.play_keyboard(me.username))
            await message.answer("✅ Приветственный пост опубликован")
        except Exception as e:
            await message.answer(f"⚠️ {html.escape(str(getattr(e, 'message', e)))}")

    @admin.message(Command("sports_refresh"))
    async def sports_refresh_cmd(message: Message) -> None:
        """Футбол: сразу обновить матчи и коэффициенты всех лиг и показать, сколько записано."""
        from . import sports as sp
        book = sp.Sportsbook(casino)
        await book.init()
        await message.answer("Обновляю матчи с ESPN…")
        try:
            n = await book.refresh_odds(force=True)
        except Exception as e:
            await message.answer(f"Ошибка: {html.escape(repr(e))}")
            return
        lines = [f"{sp.LEAGUES[k][1]}: {v}" for k, v in book.last_counts.items()]
        shown = len(await book.events())
        await message.answer(f"⚽ Записано матчей с коэффициентами: <b>{n}</b>\n" + "\n".join(lines)
                             + f"\n\nСейчас открыто для ставок в приложении: <b>{shown}</b>")

    @admin.message(Command("league_icons"))
    async def league_icons_cmd(message: Message, command: CommandObject) -> None:
        """Значки лиг в футболе: без аргументов — набор сеткой с номерами, «/league_icons epl 5» — назначить."""
        from aiogram.types import BufferedInputFile
        from . import sports as sp
        icons = sp.LeagueIcons(casino.db)
        args = (command.args or "").split()
        try:
            if len(args) == 2:
                await icons.assign(message.bot, args[0].lower(), int(args[1]))
                await message.answer(f"Значок для «{sp.LEAGUES[args[0].lower()][1]}» назначен ✅")
                return
            sheet = await icons.sheet(message.bot)
        except Exception as e:
            await message.answer(f"Не вышло: {html.escape(str(e))}")
            return
        current = await icons.mapping(message.bot)
        stickers = await icons.stickers(message.bot)
        num = {st.custom_emoji_id: i + 1 for i, st in enumerate(stickers)}
        lines = [f"{name}: <b>{num.get(current.get(k), '—')}</b>  (<code>{k}</code>)" for k, (_, name, _) in sp.LEAGUES.items()]
        await message.answer_photo(BufferedInputFile(sheet, "icons.png"), caption=(
            f"Набор <b>{sp.ICON_SET}</b>. Сейчас:\n" + "\n".join(lines)
            + "\n\nНазначить: <code>/league_icons epl 5</code>"))

    @admin.message(Command("channel_wins"))
    async def channel_wins(message: Message, command: CommandObject) -> None:
        from . import channel
        arg = (command.args or "").strip().lower()
        if arg in ("on", "off"):
            await casino.db.kv_set(channel.WINS_ON_KEY, arg)
        state = await casino.db.kv_get(channel.WINS_ON_KEY) or "on"
        await message.answer(f"Крупные выигрыши и NFT в канал: <b>{'включено' if state != 'off' else 'выключено'}</b>\n"
                             "<code>/channel_wins on</code> · <code>/channel_wins off</code>")

    @admin.message(Command("nftimg"))
    async def nft_images_check(message: Message) -> None:
        """Проверка источников картинок NFT на нескольких моделях из кейсов."""
        import aiohttp
        from .nftimg import FETCH_TIMEOUT, model_url, norm
        rows = await casino.db.all("SELECT DISTINCT collection_name, model FROM nft_models WHERE enabled=1 LIMIT 3")
        if not rows:
            await message.answer("Моделей нет — добавьте NFT релейеру или /dupe 5")
            return
        lines = ["🖼 <b>Картинки NFT</b>"]
        async with aiohttp.ClientSession(timeout=FETCH_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"}) as s:
            for r in rows:
                c, m = r["collection_name"], r["model"]
                lines.append(f"\n<b>{html.escape(c)} · {html.escape(m)}</b>")
                if relayer is None or not relayer.ready:
                    lines.append("Telegram: релейер не подключён")
                else:
                    try:
                        docs = await relayer.model_documents(c)
                        data = await relayer.model_image(c, m)
                        lines.append(f"Telegram: моделей в коллекции {len(docs)}, модель "
                                     f"{'найдена' if norm(m) in docs else 'не найдена (берём сам подарок)' if '' in docs else 'не найдена'}"
                                     f", картинка "
                                     f"{f'{len(data) // 1024} КБ' if data else 'нет'}")
                    except Exception as e:
                        lines.append(f"Telegram: ошибка {type(e).__name__}: {html.escape(str(e))[:120]}")
                try:
                    async with s.get(model_url(c, m)) as resp:
                        lines.append(f"changes.tg: HTTP {resp.status}")
                except Exception as e:
                    lines.append(f"changes.tg: {type(e).__name__}")
        await message.answer("\n".join(lines))

    @admin.message(Command("tonrate"))
    async def ton_rate(message: Message, command: CommandObject) -> None:
        arg = (command.args or "").strip()
        if arg in ("auto", "авто"):
            await casino.db.kv_set("mrkt:ton_stars", None)
        elif arg:
            try:
                value = float(arg.replace(",", "."))
                if not 1 <= value <= 100000:
                    raise ValueError
            except ValueError:
                await message.answer("Формат: <code>/tonrate 200</code> — сколько звёзд за 1 TON, или "
                                     "<code>/tonrate auto</code>")
                return
            await casino.db.kv_set("mrkt:ton_stars", str(value))
        rate = await relayer.market.ton_rate() if relayer is not None else None
        manual = await casino.db.kv_get("mrkt:ton_stars")
        await message.answer(
            f"💱 Курс: 1 TON = <b>{rate:.0f} ⭐</b> ("
            + ("задан вручную" if manual else html.escape(relayer.market.rate_source or "по цене TON из @tonprices"))
            + f", звезда = ${STAR_USD})\n"
            "Задать: <code>/tonrate 200</code>, вернуть авто: <code>/tonrate auto</code>" if rate else
            "Курс TON недоступен — задайте вручную: <code>/tonrate 200</code>")

    @admin.message(Command("nftsend"))
    async def nft_send(message: Message, command: CommandObject, bot: Bot) -> None:
        arg = (command.args or "").strip()
        if not arg.isdigit():
            await message.answer("Формат: <code>/nftsend номер_выигрыша</code>")
            return
        ok, text = await deliver_nft(bot, casino.db, cfg, relayer, int(arg))
        await message.answer(("✅ " if ok else "⚠️ ") + html.escape(text))

    @admin.message(Command("tonwallet"))
    async def ton_wallet(message: Message, command: CommandObject) -> None:
        arg = (command.args or "").strip()
        if arg in ("off", "выкл"):
            await ton.set_wallet(casino.db, None)
            await message.answer("Пополнения TON выключены.")
            return
        if arg:
            from .casino import TON_ADDRESS_RE
            if not TON_ADDRESS_RE.match(arg):
                await message.answer("Это не похоже на адрес TON-кошелька.")
                return
            await ton.set_wallet(casino.db, arg)
        address = await ton.wallet(casino.db)
        await message.answer(
            f"💎 Кошелёк для пополнений TON: <code>{html.escape(address)}</code>\n"
            "Игроки переводят на него TON с комментарием-кодом (SG + их ID) — бот сам находит перевод "
            "и зачисляет его. Переводы, пришедшие до подключения кошелька, не зачисляются.\n"
            "Сменить: <code>/tonwallet адрес</code>, выключить: <code>/tonwallet off</code>"
            if address else
            "Кошелёк не задан — пополнения TON выключены.\nЗадайте: <code>/tonwallet адрес</code> "
            "(адрес вашего TON-кошелька, например из Tonkeeper)")

    @admin.callback_query(F.data.startswith("tw:"))
    async def ton_withdraw_decision(query: CallbackQuery, bot: Bot) -> None:
        _, action, raw_id = query.data.split(":", 2)
        ok, text = await ton_decide(bot, casino, int(raw_id), query.from_user.id, action == "ok")
        await query.answer(text[:190], show_alert=not ok)
        if ok and isinstance(query.message, Message):
            try:
                await query.message.edit_text(f"{query.message.html_text}\n\n<b>{html.escape(text)}</b>",
                                              disable_web_page_preview=True)
            except Exception:
                pass

    @admin.callback_query(F.data.startswith("w:"))
    async def withdraw_decision(query: CallbackQuery, bot: Bot) -> None:
        _, action, raw_id = query.data.split(":", 2)
        wd_id = int(raw_id)
        if action == "ok":
            ok, text = await approve(bot, casino, wd_id, query.from_user.id, relayer)
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

    async def adjust(message: Message, command: CommandObject, sign: int) -> None:
        """/take и /give: игрок, сумма (или all), необязательно ton, причина. Игроку ничего не пишем."""
        name = "take" if sign < 0 else "give"
        parts = (command.args or "").split()
        if len(parts) < 2:
            await message.answer(f"Формат: <code>/{name} @username|ID сумма|all [ton] [причина]</code>\n"
                                 f"Например: <code>/{name} @vasya 500 абуз краша</code> или <code>/{name} 123 1.5 ton</code>")
            return
        user = await casino.db.find_user(parts[0])
        if not user:
            await message.answer("Игрок не найден")
            return
        rest = parts[2:]
        cur = money.STARS
        if rest and rest[0].lower() == "ton":
            cur, rest = money.TON, rest[1:]
        raw = parts[1].lower().replace(",", ".")
        if raw == "all":
            if sign > 0:
                await message.answer("«all» — только для списания")
                return
            amount = None
        else:
            try:
                value = float(raw)
            except ValueError:
                await message.answer("Сумма — число или all")
                return
            units = round(value * money.NANO) if cur == money.TON else int(value)
            if units <= 0:
                await message.answer("Сумма должна быть больше нуля")
                return
            amount = sign * units
        try:
            res = await casino.admin_adjust(message.from_user.id, user["id"], amount, cur, " ".join(rest))
        except GameError as e:
            await message.answer(html.escape(str(e)))
            return
        who = html.escape(user["first_name"] or user["username"] or str(user["id"]))
        if not res["delta"]:
            await message.answer(f"У {who} баланс {money.fmt(res['balance'], cur)} — списывать нечего.")
            return
        verb = "Списано" if res["delta"] < 0 else "Начислено"
        await message.answer(f"{verb} {money.fmt(abs(res['delta']), cur)} у {who} (<code>{user['id']}</code>). "
                             f"Баланс: {money.fmt(res['balance'], cur)}. Игрок не уведомлён.")

    @admin.message(Command("take"))
    async def take(message: Message, command: CommandObject) -> None:
        await adjust(message, command, -1)

    @admin.message(Command("give"))
    async def give(message: Message, command: CommandObject) -> None:
        await adjust(message, command, 1)

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

    # ---------- рассылка: /broadcast → кому → сообщение → превью → отправить ----------

    drafts: dict[int, dict] = {}        # админ → {"step": "list"|"content"|"confirm", "ids": [...], "who": "..."}

    def bc_cancel_kb() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="bc:cancel")]])

    @admin.message(Command("broadcast"))
    async def broadcast_start(message: Message) -> None:
        n = await bc.counts(casino.db)
        rows = [[InlineKeyboardButton(text=f"{title} · {n[key]}", callback_data=f"bc:a:{key}")]
                for key, (title, _) in bc.AUDIENCES.items()]
        rows.append([InlineKeyboardButton(text="✍️ Свой список (@ники / id)", callback_data="bc:a:list")])
        rows.append([InlineKeyboardButton(text="✖️ Отмена", callback_data="bc:cancel")])
        drafts.pop(message.from_user.id, None)
        await message.answer("📣 <b>Рассылка</b>\nКому отправить?", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    @admin.callback_query(F.data.startswith("bc:"))
    async def broadcast_step(query: CallbackQuery, bot: Bot) -> None:
        uid = query.from_user.id
        parts = query.data.split(":")
        msg = query.message if isinstance(query.message, Message) else None
        if parts[1] == "cancel":
            drafts.pop(uid, None)
            await query.answer("Рассылка отменена")
            if msg:
                await msg.edit_text("📣 Рассылка отменена.")
            return
        if parts[1] == "a":
            kind = parts[2]
            if kind == "list":
                drafts[uid] = {"step": "list"}
                text = "Пришли @ники или id получателей — через пробел, запятую или с новой строки."
            else:
                ids = await bc.audience(casino.db, kind)
                drafts[uid] = {"step": "content", "ids": ids, "who": bc.AUDIENCES[kind][0].lower()}
                text = (f"Получатели: <b>{bc.AUDIENCES[kind][0].lower()}</b> — {len(ids)}.\n"
                        "Теперь пришли сообщение для рассылки: текст, фото, видео, кружок — что угодно, "
                        "оформление сохранится.")
            await query.answer()
            if msg:
                await msg.edit_text(text, reply_markup=bc_cancel_kb())
            return
        if parts[1] == "go":
            draft = drafts.pop(uid, None)
            if not draft or draft.get("step") != "confirm":
                await query.answer("Черновик устарел — начните заново: /broadcast", show_alert=True)
                return
            await query.answer("Отправляю…")
            if msg:
                await msg.edit_text(f"⏳ Рассылка {len(draft['ids'])} получателям идёт…")

            async def run() -> None:
                res = await bc.send(bot, draft["ids"], draft["chat"], draft["message_id"],
                                    play_keyboard(cfg) if draft.get("button") else None)
                report = (f"✅ Рассылка завершена ({draft['who']}): доставлено <b>{res['sent']}</b> из {len(draft['ids'])}"
                          + (f", заблокировали бота / не запускали его: {res['blocked']}" if res["blocked"] else "")
                          + (f", ошибки: {res['failed']}" if res["failed"] else ""))
                try:
                    await bot.send_message(uid, report)
                except Exception:
                    log.warning("Отчёт о рассылке не отправлен")
            asyncio.create_task(run())
            return
        if parts[1] == "btn":
            draft = drafts.get(uid)
            if draft and draft.get("step") == "confirm":
                draft["button"] = not draft.get("button")
                await query.answer("Кнопка " + ("добавлена" if draft["button"] else "убрана"))
                if msg:
                    await msg.edit_reply_markup(reply_markup=bc_confirm_kb(draft))
            return

    def bc_confirm_kb(draft: dict) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"🚀 Отправить ({len(draft['ids'])})", callback_data="bc:go")],
            [InlineKeyboardButton(text=("✅" if draft.get("button") else "➕") + " Кнопка «Открыть Triple Gifts»",
                                  callback_data="bc:btn")],
            [InlineKeyboardButton(text="✖️ Отмена", callback_data="bc:cancel")],
        ])

    @admin.message(lambda m: m.from_user is not None and m.from_user.id in drafts
                   and drafts[m.from_user.id].get("step") in ("list", "content")
                   and not (m.text or "").startswith("/"))
    async def broadcast_input(message: Message, bot: Bot) -> None:
        uid = message.from_user.id
        draft = drafts[uid]
        if draft["step"] == "list":
            ids, missing = await bc.parse_list(casino.db, message.text or "")
            if not ids:
                await message.answer("Никого не нашёл. Пришли @ники или id ещё раз.", reply_markup=bc_cancel_kb())
                return
            drafts[uid] = {"step": "content", "ids": ids, "who": "свой список"}
            note = f"\nНе найдены: {html.escape(', '.join(missing[:20]))}" if missing else ""
            await message.answer(f"Получатели: {len(ids)}.{note}\nТеперь пришли сообщение для рассылки.",
                                 reply_markup=bc_cancel_kb())
            return
        draft.update(step="confirm", chat=message.chat.id, message_id=message.message_id,
                     button=bool(cfg.webapp_url) and not message.reply_markup)
        await bot.copy_message(chat_id=message.chat.id, from_chat_id=message.chat.id, message_id=message.message_id,
                               reply_markup=play_keyboard(cfg) if draft["button"] else None)
        await message.answer(f"👆 Так увидят игроки. Получатели: <b>{draft['who']}</b> — {len(draft['ids'])}.",
                             reply_markup=bc_confirm_kb(draft))

    root = Router(name="root")
    root.include_router(admin)
    root.include_router(router)
    return root
