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
from .nft import deliver as deliver_nft, describe as describe_nft, sync as sync_nfts
from .relayer import Relayer
from . import money, ton
from .withdraw import admin_keyboard, approve, reject, ton_admin_keyboard, ton_decide

log = logging.getLogger(__name__)

SLOT_TEXT = {"bar": "BAR", "grape": "🍇", "lemon": "🍋", "seven": "7️⃣"}

RULES = (
    "Играть можно на звёзды или на TON (переключатель ★/💎 в мини-приложении). "
    "Пополнить баланс Svag Gifts можно через Telegram Stars, TON-переводом, чеком или подарком: отправьте его аккаунту казино "
    "(мини-приложение → Кошелёк → Подарки), NFT можно ставить в PvP. "
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
            f"🎁 <b>Добро пожаловать в Svag Gifts!</b>\n\n"
            f"Слоты, краш, мины, кости, кейсы, PvP-рулетка и PvP-хоккей — в мини-приложении.\n"
            f"Вывод — подарками Telegram.\n"
            f"Баланс: <b>{user['balance']} ⭐</b>\n\n"
            f"/slot 10 — слоты прямо в чате (настоящий 🎰)\n"
            f"/deposit — пополнить звёздами\n/balance — баланс\n/ref — пригласить друзей (+10%)\n/help — правила",
            reply_markup=play_keyboard(cfg),
        )

    @router.message(Command("ref", "referral"))
    async def ref(message: Message) -> None:
        await register(message)
        me = await message.bot.get_me()
        info = await casino.referrals(message.from_user.id)
        await message.answer(
            f"🤝 <b>Реферальная программа</b>\n\nПриглашайте друзей и получайте "
            f"<b>{int(REFERRAL_RATE * 100)}%</b> от каждой их покупки звёзд — навсегда.\n"
            f"Бонус нужно один раз отыграть ставками, как чеки.\n\n"
            f"Ваша ссылка:\n{referral_link(me.username, message.from_user.id)}\n\n"
            f"Приглашено: <b>{info['count']}</b> · заработано: <b>{info['earned']} ⭐</b>",
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
        await message.answer(f"Баланс: <b>{user['balance']} ⭐</b> · <b>{money.fmt(user.get('ton') or 0, money.TON)}</b>",
                             reply_markup=play_keyboard(cfg))

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

    @router.message(Command("slot", "spin"))
    async def slot(message: Message, command: CommandObject) -> None:
        """Слоты прямо в чате: бот кидает настоящий 🎰 Telegram, выпавшее значение решает исход."""
        user = await register(message)
        arg = (command.args or "").strip()
        bet = int(arg) if arg.isdigit() else 10
        if bet < cfg.min_bet or bet > cfg.max_bet:
            await message.answer(f"Ставка — от {cfg.min_bet} до {cfg.max_bet} ⭐, например <code>/slot 10</code>")
            return
        if user["balance"] < bet:
            await message.answer(f"Недостаточно звёзд: на балансе {user['balance']} ⭐. /deposit — пополнить")
            return
        dice_msg = await message.answer_dice(emoji="🎰")
        try:
            r = await casino.slots(user["id"], bet, value=dice_msg.dice.value)
        except GameError as e:
            await message.answer(f"⚠️ {html.escape(str(e))}")
            return
        await asyncio.sleep(2.2)   # ждём, пока докрутится анимация
        combo = " ".join(SLOT_TEXT[s] for s in r["reels"])
        if r.get("nft") and r["nft"].get("demo"):
            n = r["nft"]
            text = (f"{combo}\n🎉 <b>ДЖЕКПОТ! Демо-NFT {n['emoji']} {html.escape(n['title'])} "
                    f"«{html.escape(n['model'])}» (≈ {n['price']} ⭐)</b> — он в профиле → «Мои подарки»")
        elif r.get("nft"):
            n = r["nft"]
            await deliver_nft(message.bot, casino.db, cfg, relayer, n["win_id"])
            text = (f"{combo}\n🎉 <b>ДЖЕКПОТ! NFT {n['emoji']} {html.escape(n['title'])} "
                    f"(модель «{html.escape(n['model'])}», ≈ {n['price']} ⭐)</b> — он в профиле → «Мои подарки»")
        elif r["win"] > bet:
            text = f"{combo}\n🎉 <b>×{r['multiplier']:g} — выигрыш {r['win']} ⭐</b>"
        elif r["win"] == bet:
            text = f"{combo}\nДве семёрки — ставка возвращена"
        else:
            text = f"{combo}\nМимо"
        await dice_msg.reply(f"{text}\nБаланс: {r['balance']} ⭐\nЕщё раз: <code>/slot {bet}</code>")

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
