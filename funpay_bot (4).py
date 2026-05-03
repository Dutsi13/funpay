"""
╔══════════════════════════════════════════════════════════════╗
║        FunPay AutoReply Bot + Telegram Панель управления     ║
╚══════════════════════════════════════════════════════════════╝

Установка:
    pip install FunPayPython python-telegram-bot

Как получить golden_key FunPay:
    1. Войдите на funpay.com в браузере
    2. F12 → Application → Cookies → funpay.com
    3. Скопируйте значение куки "golden_key"

Как создать Telegram-бота:
    1. Напишите @BotFather в Telegram
    2. /newbot → задайте имя → получите TOKEN
    3. Вставьте TOKEN в TG_BOT_TOKEN ниже

Как узнать свой Telegram ID:
    1. Напишите @userinfobot в Telegram
    2. Скопируйте "Id" и вставьте в TG_ADMIN_ID ниже
"""

# ════════════════════════════════════════════════════════
#  ПЕРВОНАЧАЛЬНЫЕ НАСТРОЙКИ (заполните перед запуском)
# ════════════════════════════════════════════════════════

TG_BOT_TOKEN = "8625183654:AAHbJFRw58gT9lXVeIXgLs7ryH_H8yDmyBk"   # токен от @BotFather
TG_ADMIN_ID  = 7785932103                   # ваш Telegram user_id

# ════════════════════════════════════════════════════════
#  ИМПОРТЫ
# ════════════════════════════════════════════════════════

import html
import json
import logging
import random
import threading
import time
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ════════════════════════════════════════════════════════
#  УТИЛИТА: экранирование для HTML parse_mode
#  Защищает от ошибок "Can't parse entities" когда
#  пользовательские данные содержат <, >, &, " и т.д.
# ════════════════════════════════════════════════════════

def h(text: str) -> str:
    return html.escape(str(text), quote=False)

# ════════════════════════════════════════════════════════
#  ФАЙЛ КОНФИГУРАЦИИ
# ════════════════════════════════════════════════════════

CONFIG_FILE = Path("funpay_config.json")

DEFAULT_CONFIG = {
    "golden_key": "",
    "funpay_enabled": False,
    "requests_delay": 10,
    "min_triggers": 1,
    "rules": [
        {
            "name": "Приветствие",
            "triggers": ["привет", "хай", "здравствуй", "добрый", "доброе"],
            "responses": [
                "Привет! 👋 Чем могу помочь?",
                "Здравствуйте! Слушаю вас 😊",
                "Привет! Готов ответить на ваши вопросы.",
                "Добрый день! Чем могу быть полезен?",
                "Привет! Рад вас видеть 🙂",
            ],
        },
        {
            "name": "Цена и стоимость",
            "triggers": ["цена", "стоимость", "сколько", "почём", "прайс"],
            "responses": [
                "Цены указаны в описании лота 📋",
                "Актуальные цены смотрите на странице лота.",
                "Стоимость указана в лоте. Если нужно уточнить — пишите!",
                "Цена фиксированная, указана в объявлении 👍",
                "Смотрите описание лота — там всё актуально.",
            ],
        },
        {
            "name": "Наличие товара",
            "triggers": ["наличие", "есть", "доступно", "можно", "купить"],
            "responses": [
                "Да, товар в наличии! Готов к отправке ✅",
                "В наличии есть, оформляйте заказ 🙂",
                "Есть в наличии. Напишите, если нужны подробности.",
                "Доступно к покупке! Оформляйте ✅",
                "Товар есть. Готов отправить сразу после оплаты ⚡",
            ],
        },
    ],
}


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k, v in DEFAULT_CONFIG.items():
                data.setdefault(k, v)
            return data
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()


def save_config(cfg: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


config: dict = load_config()
config_lock = threading.Lock()

# ════════════════════════════════════════════════════════
#  ЛОГИРОВАНИЕ
# ════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)s]  %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("Bot")

# ════════════════════════════════════════════════════════
#  ЛОГИКА МАТЧИНГА
# ════════════════════════════════════════════════════════

def find_response(text: str, cfg: dict) -> str | None:
    """Срабатывает если найдено >= min_triggers слов из правила (но минимум 1)."""
    text_lower = text.lower()
    min_t = max(1, cfg.get("min_triggers", 1))
    for rule in cfg.get("rules", []):
        triggers = rule.get("triggers", [])
        matched  = sum(1 for t in triggers if t.lower() in text_lower)
        if matched >= min_t:
            responses = rule.get("responses", [])
            if responses:
                return random.choice(responses[:5])
    return None

# ════════════════════════════════════════════════════════
#  ПОТОК FunPay
# ════════════════════════════════════════════════════════

funpay_thread: threading.Thread | None = None
funpay_stop_event = threading.Event()


def funpay_worker():
    try:
        from FunPayAPI import Account, Runner, enums as fp_enums
    except ImportError:
        log.error("FunPayAPI не установлен! pip install FunPayPython")
        return

    with config_lock:
        gk    = config.get("golden_key", "")
        delay = config.get("requests_delay", 5)

    if not gk:
        log.error("golden_key не задан.")
        return

    log.info("Подключение к FunPay...")
    try:
        acc = Account(gk).get()
    except Exception as e:
        log.error(f"Ошибка авторизации FunPay: {e}")
        return

    log.info(f"FunPay авторизован: {acc.username} (id={acc.id})")

    # ── MONKEY-PATCH: заглушаем get_chat_history ───────────────────────────
    # Runner вызывает этот метод при каждом новом сообщении, что приводит к
    # ошибке "Не удалось получить истории чатов". Подменяем — возвращаем [].
    _orig_gch = getattr(acc, "get_chat_history", None)
    def _safe_gch(*a, **kw):
        try:
            if _orig_gch:
                return _orig_gch(*a, **kw)
        except Exception as _e:
            log.debug(f"get_chat_history подавлен: {_e}")
        return []
    try:
        acc.get_chat_history = _safe_gch
        log.info("Monkey-patch get_chat_history активен — ошибки истории чатов подавлены.")
    except Exception:
        pass
    # ─────────────────────────────────────────────────────────────────────────

    runner = Runner(acc)

    def _safe_listen(gen):
        """Обёртка генератора: глотает исключения истории чатов, не ломает цикл."""
        while True:
            try:
                yield next(gen)
            except StopIteration:
                return
            except Exception as _e:
                _s = str(_e)
                if "истории чатов" in _s or "chat_history" in _s.lower():
                    log.warning(f"[runner] подавлено: {_e}")
                    time.sleep(2)
                    # continue — генератор продолжает работу
                else:
                    raise

    retry_delay = 15
    while not funpay_stop_event.is_set():
        try:
            raw_gen = runner.listen(requests_delay=delay)
            for event in _safe_listen(raw_gen):
                if funpay_stop_event.is_set():
                    log.info("FunPay поток остановлен.")
                    return
                try:
                    if event.type is fp_enums.EventTypes.NEW_MESSAGE:
                        msg = event.message
                        if msg.author_id == acc.id or not msg.text:
                            continue
                        log.info(f"[FunPay] chat={msg.chat_id} | {msg.author}: {msg.text!r}")
                        with config_lock:
                            cfg_snap = json.loads(json.dumps(config))
                        response = find_response(msg.text, cfg_snap)
                        if response:
                            time.sleep(random.uniform(1.5, 3.0))
                            try:
                                acc.send_message(msg.chat_id, response)
                                log.info(f"  -> {response!r}")
                            except Exception as send_err:
                                log.error(f"Ошибка отправки: {send_err}")

                    elif event.type is fp_enums.EventTypes.NEW_ORDER:
                        order = event.order
                        log.info(f"[FunPay] Новый заказ #{order.id} от {order.buyer_username}")
                        try:
                            chat = acc.get_chat_by_name(order.buyer_username, create=True)
                            greeting = (
                                f"Привет, {order.buyer_username}! 👋\n"
                                f"Получил ваш заказ #{order.id}. Сейчас всё подготовлю!"
                            )
                            time.sleep(random.uniform(1.5, 3.0))
                            acc.send_message(chat.id, greeting)
                        except Exception as e:
                            log.error(f"Ошибка приветствия: {e}")
                except Exception as e:
                    log.error(f"Ошибка обработки события: {e}")
        except Exception as outer_err:
            if funpay_stop_event.is_set():
                return
            log.error(f"Ошибка FunPay runner: {outer_err}. Повтор через {retry_delay} сек...")
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 120)
        else:
            retry_delay = 15


def start_funpay() -> bool:
    global funpay_thread, funpay_stop_event
    if funpay_thread and funpay_thread.is_alive():
        return False
    funpay_stop_event.clear()
    funpay_thread = threading.Thread(target=funpay_worker, daemon=True)
    funpay_thread.start()
    return True


def stop_funpay():
    funpay_stop_event.set()

# ════════════════════════════════════════════════════════
#  СОСТОЯНИЯ ДИАЛОГА
# ════════════════════════════════════════════════════════

(
    ST_MAIN, ST_SET_KEY, ST_SET_DELAY, ST_SET_MIN_TRIGGERS,
    ST_RULE_MENU, ST_RULE_NAME, ST_RULE_TRIGGERS, ST_RULE_RESPONSES,
    ST_EDIT_RULE, ST_EDIT_WHAT,
) = range(10)

# ════════════════════════════════════════════════════════
#  КЛАВИАТУРЫ
# ════════════════════════════════════════════════════════

def kb_main(cfg: dict) -> InlineKeyboardMarkup:
    running = cfg.get("funpay_enabled") and cfg.get("golden_key")
    status  = "🟢 Работает" if running else "🔴 Остановлен"
    toggle  = "⏹ Остановить FunPay" if running else "▶️ Запустить FunPay"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Статус: {status}", callback_data="noop")],
        [InlineKeyboardButton(toggle,               callback_data="toggle_funpay")],
        [InlineKeyboardButton("🔑 Golden Key",      callback_data="set_key"),
         InlineKeyboardButton("⏱ Задержка",         callback_data="set_delay")],
        [InlineKeyboardButton("🎯 Мин. триггеров",  callback_data="set_min_triggers")],
        [InlineKeyboardButton("📋 Правила",          callback_data="rules_menu")],
        [InlineKeyboardButton("📊 Статистика",       callback_data="stats")],
    ])


def kb_rules(cfg: dict) -> InlineKeyboardMarkup:
    rows = []
    for i, rule in enumerate(cfg.get("rules", [])):
        rows.append([InlineKeyboardButton(f"✏️ {rule['name']}", callback_data=f"edit_rule_{i}")])
    rows.append([InlineKeyboardButton("➕ Добавить правило", callback_data="add_rule")])
    rows.append([InlineKeyboardButton("🗑 Удалить правило",  callback_data="del_rule")])
    rows.append([InlineKeyboardButton("🔙 Главное меню",     callback_data="back_main")])
    return InlineKeyboardMarkup(rows)


def kb_edit_rule(idx: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Название",   callback_data=f"ewr_name_{idx}")],
        [InlineKeyboardButton("🎯 Триггеры",   callback_data=f"ewr_triggers_{idx}")],
        [InlineKeyboardButton("💬 Ответы",     callback_data=f"ewr_responses_{idx}")],
        [InlineKeyboardButton("🔙 К правилам", callback_data="rules_menu")],
    ])


def kb_del_rules(cfg: dict) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"🗑 {r['name']}", callback_data=f"confirm_del_{i}")]
            for i, r in enumerate(cfg.get("rules", []))]
    rows.append([InlineKeyboardButton("🔙 Назад", callback_data="rules_menu")])
    return InlineKeyboardMarkup(rows)

# ════════════════════════════════════════════════════════
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ════════════════════════════════════════════════════════

def is_admin(update: Update) -> bool:
    return (update.effective_user.id if update.effective_user else None) == TG_ADMIN_ID


def cfg_snapshot() -> dict:
    with config_lock:
        return json.loads(json.dumps(config))


def main_menu_text(cfg: dict) -> str:
    gk = cfg.get("golden_key", "")
    # Показываем только последние 4 символа, всё экранируем через h()
    if len(gk) > 4:
        gk_preview = "••••••" + h(gk[-4:])
    elif gk:
        gk_preview = h(gk)
    else:
        gk_preview = "<i>не задан</i>"
    return (
        "🤖 <b>FunPay AutoReply — Панель управления</b>\n\n"
        f"• Golden Key: <code>{gk_preview}</code>\n"
        f"• Задержка запросов: <code>{h(str(cfg['requests_delay']))} сек</code>\n"
        f"• Мин. совпавших триггеров: <code>{h(str(cfg['min_triggers']))}</code>\n"
        f"• Правил автоответа: <code>{len(cfg['rules'])}</code>"
    )

# ════════════════════════════════════════════════════════
#  HANDLER: /start
# ════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update):
        await update.message.reply_text("⛔ Доступ запрещён.")
        return ConversationHandler.END
    cfg = cfg_snapshot()
    await update.message.reply_text(
        main_menu_text(cfg), parse_mode="HTML", reply_markup=kb_main(cfg)
    )
    return ST_MAIN

# ════════════════════════════════════════════════════════
#  HANDLER: главное меню
# ════════════════════════════════════════════════════════

async def cb_main(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "noop":
        return ST_MAIN

    if data == "back_main":
        cfg = cfg_snapshot()
        await query.edit_message_text(
            main_menu_text(cfg), parse_mode="HTML", reply_markup=kb_main(cfg)
        )
        return ST_MAIN

    if data == "toggle_funpay":
        with config_lock:
            enabled = config.get("funpay_enabled")
            gk      = config.get("golden_key", "")
            if not gk:
                await query.answer("Сначала задайте Golden Key!", show_alert=True)
                return ST_MAIN
            if enabled:
                config["funpay_enabled"] = False
                save_config(config)
                stop_funpay()
                msg = "⏹ FunPay <b>остановлен</b>."
            else:
                config["funpay_enabled"] = True
                save_config(config)
                ok  = start_funpay()
                msg = "▶️ FunPay <b>запущен</b>!" if ok else "⚠️ Уже работает."
            cfg = json.loads(json.dumps(config))
        await query.edit_message_text(msg, parse_mode="HTML", reply_markup=kb_main(cfg))
        return ST_MAIN

    if data == "stats":
        lines = ["📊 <b>Правила автоответа:</b>\n"]
        cfg = cfg_snapshot()
        for i, rule in enumerate(cfg["rules"], 1):
            tlist = ", ".join(h(t) for t in rule.get("triggers", []))
            lines.append(
                f"{i}. <b>{h(rule['name'])}</b> — "
                f"триггеров: {len(rule.get('triggers', []))}, "
                f"ответов: {len(rule.get('responses', []))}"
            )
            lines.append(f"   <code>{tlist}</code>")
        await query.edit_message_text(
            "\n".join(lines), parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Назад", callback_data="back_main")
            ]])
        )
        return ST_MAIN

    if data == "set_key":
        await query.edit_message_text(
            "🔑 Введите ваш <b>golden_key</b> с сайта FunPay:\n\n"
            "<i>Как получить: F12 → Application → Cookies → funpay.com → golden_key</i>",
            parse_mode="HTML"
        )
        return ST_SET_KEY

    if data == "set_delay":
        cfg = cfg_snapshot()
        await query.edit_message_text(
            f"⏱ Текущая задержка: <b>{h(str(cfg['requests_delay']))} сек</b>\n\n"
            "Введите новое значение в секундах (минимум 3):",
            parse_mode="HTML"
        )
        return ST_SET_DELAY

    if data == "set_min_triggers":
        cfg = cfg_snapshot()
        await query.edit_message_text(
            f"🎯 Сейчас срабатывает при <b>{h(str(cfg['min_triggers']))}</b> совпавших триггерах.\n\n"
            "Введите число (от 1 до 10):\n"
            "<i>Рекомендуется: 1 — бот ответит на любое триггер-слово</i>",
            parse_mode="HTML"
        )
        return ST_SET_MIN_TRIGGERS

    if data == "rules_menu":
        cfg = cfg_snapshot()
        await query.edit_message_text(
            "📋 <b>Правила автоответа</b>\n\nВыберите правило для редактирования:",
            parse_mode="HTML", reply_markup=kb_rules(cfg)
        )
        return ST_RULE_MENU

    return ST_MAIN

# ════════════════════════════════════════════════════════
#  HANDLERS: ввод настроек
# ════════════════════════════════════════════════════════

async def handle_set_key(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    key = update.message.text.strip()
    with config_lock:
        config["golden_key"] = key
        save_config(config)
        cfg = json.loads(json.dumps(config))
    # Никогда не вставляем key напрямую — только через h()
    preview = ("••••••" + h(key[-4:])) if len(key) > 4 else h(key)
    await update.message.reply_text(
        f"✅ Golden Key сохранён: <code>{preview}</code>\n\n"
        "Теперь запустите FunPay через главное меню.",
        parse_mode="HTML", reply_markup=kb_main(cfg)
    )
    return ST_MAIN


async def handle_set_delay(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        val = max(3, int(update.message.text.strip()))
    except ValueError:
        await update.message.reply_text(
            "❗ Введите целое число, например: <code>5</code>", parse_mode="HTML"
        )
        return ST_SET_DELAY
    with config_lock:
        config["requests_delay"] = val
        save_config(config)
        cfg = json.loads(json.dumps(config))
    await update.message.reply_text(
        f"✅ Задержка: <b>{val} сек</b>", parse_mode="HTML", reply_markup=kb_main(cfg)
    )
    return ST_MAIN


async def handle_set_min_triggers(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        val = max(1, min(10, int(update.message.text.strip())))
    except ValueError:
        await update.message.reply_text("❗ Введите число от 1 до 10.", parse_mode="HTML")
        return ST_SET_MIN_TRIGGERS
    with config_lock:
        config["min_triggers"] = val
        save_config(config)
        cfg = json.loads(json.dumps(config))
    await update.message.reply_text(
        f"✅ Срабатывание при <b>{val}</b> совпавших триггерах.",
        parse_mode="HTML", reply_markup=kb_main(cfg)
    )
    return ST_MAIN

# ════════════════════════════════════════════════════════
#  HANDLERS: меню правил
# ════════════════════════════════════════════════════════

async def cb_rules_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    cfg  = cfg_snapshot()

    if data == "back_main":
        await query.edit_message_text(
            main_menu_text(cfg), parse_mode="HTML", reply_markup=kb_main(cfg)
        )
        return ST_MAIN

    if data == "rules_menu":
        await query.edit_message_text(
            "📋 <b>Правила автоответа</b>",
            parse_mode="HTML", reply_markup=kb_rules(cfg)
        )
        return ST_RULE_MENU

    if data == "add_rule":
        ctx.user_data["adding_rule"] = {}
        await query.edit_message_text(
            "➕ <b>Новое правило</b>\n\n"
            "Шаг 1/3. Введите <b>название</b> правила\n"
            "Пример: <i>Вопрос о доставке</i>",
            parse_mode="HTML"
        )
        return ST_RULE_NAME

    if data == "del_rule":
        if not cfg["rules"]:
            await query.answer("Нет правил для удаления.", show_alert=True)
            return ST_RULE_MENU
        await query.edit_message_text(
            "🗑 Выберите правило для удаления:",
            reply_markup=kb_del_rules(cfg)
        )
        return ST_RULE_MENU

    if data.startswith("confirm_del_"):
        idx  = int(data.split("_")[-1])
        name = ""
        with config_lock:
            if 0 <= idx < len(config["rules"]):
                name = config["rules"].pop(idx)["name"]
                save_config(config)
                cfg = json.loads(json.dumps(config))
        await query.edit_message_text(
            f"🗑 Правило <b>{h(name)}</b> удалено.",
            parse_mode="HTML", reply_markup=kb_rules(cfg)
        )
        return ST_RULE_MENU

    if data.startswith("edit_rule_"):
        idx = int(data.split("_")[-1])
        ctx.user_data["editing_rule_idx"] = idx
        if idx >= len(cfg["rules"]):
            await query.answer("Правило не найдено.", show_alert=True)
            return ST_RULE_MENU
        rule  = cfg["rules"][idx]
        tlist = ", ".join(h(t) for t in rule.get("triggers", []))
        rlist = "\n".join(
            f"  {i+1}. {h(r)}" for i, r in enumerate(rule.get("responses", []))
        )
        text = (
            f"✏️ <b>{h(rule['name'])}</b>\n\n"
            f"Триггеры: <code>{tlist}</code>\n\n"
            f"Ответы ({len(rule.get('responses', []))}):\n{rlist}"
        )
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb_edit_rule(idx))
        return ST_EDIT_RULE

    if data.startswith("ewr_name_"):
        idx = int(data.split("_")[-1])
        ctx.user_data["editing_rule_idx"] = idx
        ctx.user_data["editing_field"]    = "name"
        await query.edit_message_text(
            f"✏️ Введите новое <b>название</b> для правила #{idx+1}:",
            parse_mode="HTML"
        )
        return ST_EDIT_WHAT

    if data.startswith("ewr_triggers_"):
        idx = int(data.split("_")[-1])
        ctx.user_data["editing_rule_idx"] = idx
        ctx.user_data["editing_field"]    = "triggers"
        await query.edit_message_text(
            "🎯 Введите <b>триггер-слова</b> через запятую:\n\n"
            "Пример: <code>привет, хай, здравствуй, добрый, хелло</code>\n\n"
            f"Мин. совпадений для срабатывания: <b>{cfg['min_triggers']}</b>\n"
            "<i>При значении 1 достаточно одного слова из списка</i>",
            parse_mode="HTML"
        )
        return ST_EDIT_WHAT

    if data.startswith("ewr_responses_"):
        idx = int(data.split("_")[-1])
        ctx.user_data["editing_rule_idx"] = idx
        ctx.user_data["editing_field"]    = "responses"
        await query.edit_message_text(
            "💬 Введите <b>ответные фразы</b> — каждую с новой строки (максимум 5):\n\n"
            "Пример:\n"
            "<code>Привет! Чем помочь?\n"
            "Здравствуйте! Слушаю.\n"
            "Добрый день! 😊</code>",
            parse_mode="HTML"
        )
        return ST_EDIT_WHAT

    return ST_RULE_MENU


async def handle_rule_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    name = update.message.text.strip()
    ctx.user_data["adding_rule"]["name"] = name
    await update.message.reply_text(
        f"✅ Название: <b>{h(name)}</b>\n\n"
        "Шаг 2/3. Введите <b>триггер-слова</b> через запятую (рекомендуется 5–8 слов):\n\n"
        "Пример: <code>цена, стоимость, сколько, почём, прайс</code>",
        parse_mode="HTML"
    )
    return ST_RULE_TRIGGERS


async def handle_rule_triggers(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    raw      = update.message.text.strip()
    triggers = [t.strip().lower() for t in raw.split(",") if t.strip()]
    ctx.user_data["adding_rule"]["triggers"] = triggers
    await update.message.reply_text(
        f"✅ Триггеров добавлено: <b>{len(triggers)}</b>\n\n"
        "Шаг 3/3. Введите <b>ответные фразы</b> — каждую с новой строки (максимум 5):\n\n"
        "Пример:\n<code>Да, в наличии!\nЕсть. Оформляйте.</code>",
        parse_mode="HTML"
    )
    return ST_RULE_RESPONSES


async def handle_rule_responses(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    raw       = update.message.text.strip()
    responses = [r.strip() for r in raw.splitlines() if r.strip()][:5]
    rule      = ctx.user_data.pop("adding_rule", {})
    rule["responses"] = responses
    with config_lock:
        config["rules"].append(rule)
        save_config(config)
        cfg = json.loads(json.dumps(config))
    await update.message.reply_text(
        f"✅ Правило <b>{h(rule.get('name', '—'))}</b> добавлено!\n"
        f"• Триггеров: {len(rule.get('triggers', []))}\n"
        f"• Ответов: {len(responses)}",
        parse_mode="HTML", reply_markup=kb_rules(cfg)
    )
    return ST_RULE_MENU


async def handle_edit_field(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    idx   = ctx.user_data.get("editing_rule_idx", 0)
    field = ctx.user_data.get("editing_field", "")
    raw   = update.message.text.strip()
    with config_lock:
        if 0 <= idx < len(config["rules"]):
            if field == "name":
                config["rules"][idx]["name"] = raw
            elif field == "triggers":
                config["rules"][idx]["triggers"] = [
                    t.strip().lower() for t in raw.split(",") if t.strip()
                ]
            elif field == "responses":
                config["rules"][idx]["responses"] = [
                    r.strip() for r in raw.splitlines() if r.strip()
                ][:5]
            save_config(config)
        cfg = json.loads(json.dumps(config))
    await update.message.reply_text("✅ Сохранено!", reply_markup=kb_rules(cfg))
    return ST_RULE_MENU

# ════════════════════════════════════════════════════════
#  CANCEL
# ════════════════════════════════════════════════════════

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    cfg = cfg_snapshot()
    await update.message.reply_text("❌ Отменено.", reply_markup=kb_main(cfg))
    return ST_MAIN

# ════════════════════════════════════════════════════════
#  ЗАПУСК
# ════════════════════════════════════════════════════════

def main():
    with config_lock:
        if config.get("funpay_enabled") and config.get("golden_key"):
            log.info("Автозапуск FunPay...")
            start_funpay()

    app = Application.builder().token(TG_BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            ST_MAIN: [
                CallbackQueryHandler(cb_main),
            ],
            ST_SET_KEY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_set_key),
            ],
            ST_SET_DELAY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_set_delay),
            ],
            ST_SET_MIN_TRIGGERS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_set_min_triggers),
            ],
            ST_RULE_MENU: [
                CallbackQueryHandler(cb_rules_menu),
            ],
            ST_EDIT_RULE: [
                CallbackQueryHandler(cb_rules_menu),
            ],
            ST_RULE_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_rule_name),
            ],
            ST_RULE_TRIGGERS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_rule_triggers),
            ],
            ST_RULE_RESPONSES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_rule_responses),
            ],
            ST_EDIT_WHAT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_edit_field),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            CommandHandler("start",  cmd_start),
        ],
        per_message=False,
    )

    app.add_handler(conv)
    log.info("Telegram-бот запущен. Откройте чат с ботом и отправьте /start")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
