import os
import json
import re
import asyncio
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
)
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext

from openai import AsyncOpenAI

# ---------- Timezone (with fallback) ----------
from zoneinfo import ZoneInfo
try:
    TZ = ZoneInfo("Asia/Tbilisi")
except Exception:
    TZ = ZoneInfo("UTC")


# ================== CONFIG ==================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

CHANNEL_1 = os.getenv("CHANNEL_1", "@po_chashchinski")
CHANNEL_2 = os.getenv("CHANNEL_2", "@newshiftspace")
CHANNEL_1_URL = os.getenv("CHANNEL_1_URL", "https://t.me/po_chashchinski")
CHANNEL_2_URL = os.getenv("CHANNEL_2_URL", "https://t.me/newshiftspace")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

USAGE_FILE = "usage.json"
USERS_FILE = "users.json"

MAX_RUNS_PER_DAY = 2
WELCOME_IMAGE_PATH = "ewr.png"  # рядом с main.py

QUESTIONS = [
    "1) Чем вы занимаетесь, расскажите что умеете?",
    "2) Если бы вам не нужно было работать, то чем бы вы хотели заниматься?",
    "3) Вы хотите быстрый результат или готовы работать в долгосрочную перспективу?",
    "4) Расскажите о вашем хобби.",
    "5) Вы командный игрок или одиночка?",
    "6) Оцените свои навыки в работе с ИИ по 10 бальной шкале.",
]

GREETING = (
    "Привет! Я твой личный ИИ-помощник 🤖\n"
    "Помогу найти бизнес-идею, отталкиваясь от твоих интересов.\n\n"
    "Нажми «🔎 Найти бизнес идею» — начнём."
)

ABOUT_TEXT = (
    "ℹ️ О боте\n\n"
    "Я задаю 6 вопросов и генерирую 3 необычные бизнес-идеи под твой профиль.\n"
    "Минимум воды — максимум конкретики: первые шаги + монетизация."
)

COOP_TEXT = (
    "🤝 Сотрудничество\n\n"
    "По вопросам сотрудничества (реклама/интеграция/партнёрство/разработка) - писать @NukutaC\n"
)

# ================== KEYBOARDS ==================
def main_menu_kb(is_admin: bool) -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton(text="🔎 Найти бизнес идею")],
        [KeyboardButton(text="ℹ️ О боте"), KeyboardButton(text="🤝 Сотрудничество")],
    ]
    if is_admin:
        keyboard.append([KeyboardButton(text="📊 Статистика"), KeyboardButton(text="📣 Рассылка")])

    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        input_field_placeholder="Выберите действие…",
    )

def subscribe_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подписаться на канал 1", url=CHANNEL_1_URL)],
            [InlineKeyboardButton(text="✅ Подписаться на канал 2", url=CHANNEL_2_URL)],
            [InlineKeyboardButton(text="🔄 Проверить подписку", callback_data="check_sub")],
            [InlineKeyboardButton(text="🏠 В меню", callback_data="go_menu")],
        ]
    )

def result_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔎 Новая идея", callback_data="start_find")],
            [InlineKeyboardButton(text="🏠 В меню", callback_data="go_menu")],
        ]
    )

# ================== STATES ==================
class Form(StatesGroup):
    q1 = State()
    q2 = State()
    q3 = State()
    q4 = State()
    q5 = State()
    q6 = State()

class AdminFlow(StatesGroup):
    broadcast_wait_text = State()

router = Router()

# ================== HELPERS ==================
async def safe_delete(bot: Bot, chat_id: int, message_id: int):
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass

def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")

def today_key() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")

def is_admin(user_id: int) -> bool:
    return ADMIN_ID != 0 and user_id == ADMIN_ID

# ================== USERS STORAGE (/start list) ==================
_users_lock = asyncio.Lock()

async def load_users() -> Dict[str, Any]:
    if not os.path.exists(USERS_FILE):
        return {}
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

async def save_users(data: Dict[str, Any]):
    tmp = USERS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, USERS_FILE)

async def upsert_user(user_id: int):
    async with _users_lock:
        data = await load_users()
        uid = str(user_id)
        ts = now_iso()
        if uid not in data:
            data[uid] = {"first_seen": ts, "last_start": ts}
        else:
            data[uid]["last_start"] = ts
        await save_users(data)

async def remove_user(user_id: int):
    async with _users_lock:
        data = await load_users()
        uid = str(user_id)
        if uid in data:
            del data[uid]
            await save_users(data)

# ================== DAILY LIMIT (2/day) ==================
_usage_lock = asyncio.Lock()

async def load_usage() -> Dict[str, Any]:
    if not os.path.exists(USAGE_FILE):
        return {}
    try:
        with open(USAGE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

async def save_usage(data: Dict[str, Any]):
    tmp = USAGE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, USAGE_FILE)

async def get_remaining_runs(user_id: int) -> int:
    async with _usage_lock:
        data = await load_usage()
        day = today_key()
        used = int(data.get(str(user_id), {}).get(day, 0))
        return max(0, MAX_RUNS_PER_DAY - used)

async def increment_runs(user_id: int):
    async with _usage_lock:
        data = await load_usage()
        day = today_key()
        uid = str(user_id)
        if uid not in data:
            data[uid] = {}
        data[uid][day] = int(data[uid].get(day, 0)) + 1
        await save_usage(data)

# ================== SUBSCRIPTION CHECK ==================
async def is_subscribed(bot: Bot, user_id: int, channel: str) -> bool:
    if not channel:
        return True
    try:
        member = await bot.get_chat_member(chat_id=channel, user_id=user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception:
        return False

async def check_both_subscriptions(bot: Bot, user_id: int) -> bool:
    ok1 = await is_subscribed(bot, user_id, CHANNEL_1)
    ok2 = await is_subscribed(bot, user_id, CHANNEL_2)
    return ok1 and ok2

# ================== OPENAI ==================
openai_client: Optional[AsyncOpenAI] = None

def format_ideas(ideas: List[Dict[str, Any]], remaining_after: int) -> str:
    out = ["🔥 Вот 3 бизнес-идеи под тебя:\n"]
    for i, idea in enumerate(ideas, 1):
        steps = "\n".join([f"• {s}" for s in idea.get("steps", [])])
        out.append(
            f"**Идея {i}: {idea.get('title','').strip()}**\n"
            f"Почему: {idea.get('why','').strip()}\n\n"
            f"Первые шаги:\n{steps}\n\n"
            f"Монетизация: {idea.get('money','').strip()}\n"
        )
    out.append(f"\n⏳ Осталось на сегодня: {remaining_after}/{MAX_RUNS_PER_DAY}")
    return "\n".join(out).strip()

async def generate_ideas_with_openai(answers: Dict[str, str]) -> List[Dict[str, Any]]:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY missing in .env")

    prompt = f"""
Ты — генератор НЕОБЫЧНЫХ бизнес-идей.

Сделай РОВНО 3 идеи, максимально подходящие человеку по ответам ниже.

Жёсткие требования:
- Никаких банальностей: не предлагай "агентство", "SMM", "чатбот для всех", "просто курс", "дропшиппинг", "коучинг без ниши".
- У каждой идеи должен быть twist: неочевидное сочетание навыков/хобби/аудитории/формата.
- Без серых/нелегальных схем.
- Каждая идея реалистична для старта маленькими шагами.

Верни ответ СТРОГО как JSON массив из 3 объектов, без любого текста вокруг.
Схема:
[
  {{
    "title": "…",
    "why_fit": "1-2 предложения",
    "first_steps": ["шаг 1", "шаг 2", "шаг 3"],
    "monetization": "1 строка"
  }},
  ...
]

Ответы пользователя:
1) {answers.get("q1","")}
2) {answers.get("q2","")}
3) {answers.get("q3","")}
4) {answers.get("q4","")}
5) {answers.get("q5","")}
6) {answers.get("q6","")}
""".strip()

    last_text = ""
    for attempt in range(2):
        resp = await openai_client.responses.create(
            model=OPENAI_MODEL,
            input=prompt if attempt == 0 else (prompt + "\n\nПовтори: верни ТОЛЬКО JSON массив, без текста."),
        )
        text = (resp.output_text or "").strip()
        last_text = text

        try:
            data = json.loads(text)
        except Exception:
            m = re.search(r"(\[.*\])", text, flags=re.DOTALL)
            if not m:
                continue
            data = json.loads(m.group(1))

        ideas: List[Dict[str, Any]] = []
        for item in (data or [])[:3]:
            ideas.append({
                "title": str(item.get("title", "")).strip(),
                "why": str(item.get("why_fit", "")).strip(),
                "steps": list(item.get("first_steps", []) or [])[:3],
                "money": str(item.get("monetization", "")).strip(),
            })

        if len(ideas) == 3 and all(x["title"] for x in ideas):
            return ideas

    raise RuntimeError(f"Не удалось разобрать JSON от ИИ. Ответ:\n{last_text[:800]}")

# ================== FLOW ==================
async def start_questions(bot: Bot, chat_id: int, state: FSMContext):
    await state.set_state(Form.q1)
    await bot.send_message(chat_id, "Отлично! Начнём.\n\n" + QUESTIONS[0])

async def start_find_flow(bot: Bot, chat_id: int, user_id: int, state: FSMContext):
    await state.clear()

    remaining = await get_remaining_runs(user_id)
    if remaining <= 0:
        await bot.send_message(
            chat_id,
            f"⛔ Лимит: {MAX_RUNS_PER_DAY} поиска в день.\n"
            f"Сегодня ({today_key()}) лимит исчерпан.\n\n"
            f"Попробуй завтра 🙂",
            reply_markup=result_kb()
        )
        return

    if await check_both_subscriptions(bot, user_id):
        await start_questions(bot, chat_id, state)
        return

    text = (
        "Перед стартом подпишись на 2 канала ✅\n"
        "После подписки нажми «🔄 Проверить подписку».\n\n"
        "Если ты уже подписан — нажми проверку."
    )
    await bot.send_message(chat_id, text, reply_markup=subscribe_kb())

# ================== /START ==================
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, bot: Bot):
    await state.clear()

    # фиксируем пользователя для будущей рассылки
    await upsert_user(message.from_user.id)

    admin_flag = is_admin(message.from_user.id)

    try:
        photo = FSInputFile(WELCOME_IMAGE_PATH)
        await bot.send_photo(
            chat_id=message.chat.id,
            photo=photo,
            caption=GREETING,
            reply_markup=main_menu_kb(admin_flag)
        )
    except Exception:
        await bot.send_message(
            chat_id=message.chat.id,
            text=GREETING,
            reply_markup=main_menu_kb(admin_flag)
        )

# ================== MENU BUTTON HANDLERS ==================
@router.message(F.text == "ℹ️ О боте")
async def about(message: Message, bot: Bot):
    await safe_delete(bot, message.chat.id, message.message_id)
    await bot.send_message(message.chat.id, ABOUT_TEXT)

@router.message(F.text == "🤝 Сотрудничество")
async def coop(message: Message, bot: Bot):
    await safe_delete(bot, message.chat.id, message.message_id)
    await bot.send_message(message.chat.id, COOP_TEXT)

@router.message(F.text == "🔎 Найти бизнес идею")
async def find_idea(message: Message, state: FSMContext, bot: Bot):
    await safe_delete(bot, message.chat.id, message.message_id)
    await start_find_flow(bot, message.chat.id, message.from_user.id, state)

# ================== SUBSCRIBE CALLBACKS ==================
@router.callback_query(F.data == "go_menu")
async def cb_go_menu(call: CallbackQuery, state: FSMContext, bot: Bot):
    await call.answer()
    await state.clear()
    admin_flag = is_admin(call.from_user.id)
    await bot.send_message(call.message.chat.id, "Ок, выбери действие в меню 👇", reply_markup=main_menu_kb(admin_flag))

@router.callback_query(F.data == "start_find")
async def cb_start_find(call: CallbackQuery, state: FSMContext, bot: Bot):
    await call.answer()
    await start_find_flow(bot, call.message.chat.id, call.from_user.id, state)

@router.callback_query(F.data == "check_sub")
async def cb_check_sub(call: CallbackQuery, state: FSMContext, bot: Bot):
    await call.answer()

    remaining = await get_remaining_runs(call.from_user.id)
    if remaining <= 0:
        await bot.send_message(
            call.message.chat.id,
            f"⛔ Лимит: {MAX_RUNS_PER_DAY} поиска в день.\n"
            f"Сегодня ({today_key()}) лимит исчерпан.\n\n"
            f"Попробуй завтра 🙂",
            reply_markup=result_kb()
        )
        await state.clear()
        return

    ok = await check_both_subscriptions(bot, call.from_user.id)
    if not ok:
        await call.answer("Не вижу подписку на оба канала 😕", show_alert=True)
        return

    await start_questions(bot, call.message.chat.id, state)

# ================== QUESTIONS (Q->A stays) ==================
async def handle_answer(
    message: Message,
    state: FSMContext,
    bot: Bot,
    key: str,
    next_state: Optional[State],
    next_question: Optional[str],
):
    await state.update_data(**{key: (message.text or "").strip()})
    if next_state and next_question:
        await state.set_state(next_state)
        await bot.send_message(message.chat.id, next_question)

@router.message(Form.q1)
async def q1(message: Message, state: FSMContext, bot: Bot):
    await handle_answer(message, state, bot, "q1", Form.q2, QUESTIONS[1])

@router.message(Form.q2)
async def q2(message: Message, state: FSMContext, bot: Bot):
    await handle_answer(message, state, bot, "q2", Form.q3, QUESTIONS[2])

@router.message(Form.q3)
async def q3(message: Message, state: FSMContext, bot: Bot):
    await handle_answer(message, state, bot, "q3", Form.q4, QUESTIONS[3])

@router.message(Form.q4)
async def q4(message: Message, state: FSMContext, bot: Bot):
    await handle_answer(message, state, bot, "q4", Form.q5, QUESTIONS[4])

@router.message(Form.q5)
async def q5(message: Message, state: FSMContext, bot: Bot):
    await handle_answer(message, state, bot, "q5", Form.q6, QUESTIONS[5])

@router.message(Form.q6)
async def q6(message: Message, state: FSMContext, bot: Bot):
    await state.update_data(q6=(message.text or "").strip())

    data = await state.get_data()
    answers = {k: data.get(k, "") for k in ["q1", "q2", "q3", "q4", "q5", "q6"]}

    await bot.send_message(message.chat.id, "🧠 Думаю и собираю 3 идеи под твой профиль…")

    remaining_before = await get_remaining_runs(message.from_user.id)
    if remaining_before <= 0:
        await bot.send_message(
            message.chat.id,
            f"⛔ Лимит: {MAX_RUNS_PER_DAY} поиска в день.\n"
            f"Сегодня ({today_key()}) лимит исчерпан.\n\n"
            f"Попробуй завтра 🙂",
            reply_markup=result_kb()
        )
        await state.clear()
        return

    try:
        ideas = await generate_ideas_with_openai(answers)
    except Exception as e:
        await bot.send_message(
            message.chat.id,
            "⚠️ Не получилось получить идеи от ИИ.\n\n"
            f"Ошибка: {str(e)[:600]}\n\n"
            "Попробуй ещё раз чуть позже."
        )
        await state.clear()
        return

    await increment_runs(message.from_user.id)
    remaining_after = await get_remaining_runs(message.from_user.id)

    await bot.send_message(
        message.chat.id,
        format_ideas(ideas, remaining_after),
        reply_markup=result_kb()
    )

    await state.clear()

# ================== ADMIN: MYID (optional debug) ==================
@router.message(Command("myid"))
async def myid_cmd(message: Message):
    await message.answer(f"Ваш ID: {message.from_user.id}\nADMIN_ID из .env: {ADMIN_ID}")

# ================== ADMIN: STATS ==================
@router.message(F.text == "📊 Статистика")
async def admin_stats_button(message: Message, bot: Bot):
    if not is_admin(message.from_user.id):
        return

    await safe_delete(bot, message.chat.id, message.message_id)

    users = await load_users()
    usage = await load_usage()

    today = today_key()
    now = datetime.now(TZ)
    days30 = [(now.date() - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(0, 30)]
    days7 = [(now.date() - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(0, 7)]

    total_users = len(users)

    new_today = 0
    started_today = 0
    for uid, info in users.items():
        fs = str(info.get("first_seen", ""))[:10]
        ls = str(info.get("last_start", ""))[:10]
        if fs == today:
            new_today += 1
        if ls == today:
            started_today += 1

    dau_users = 0
    runs_today = 0
    mau_set = set()
    runs_7d = 0

    for uid, daymap in usage.items():
        if today in daymap:
            dau_users += 1
            runs_today += int(daymap.get(today, 0))

        for d in days30:
            if d in daymap:
                mau_set.add(uid)
                break

        for d in days7:
            if d in daymap:
                runs_7d += int(daymap.get(d, 0))

    mau = len(mau_set)

    text = (
        "📊 Статистика\n\n"
        f"👥 Всего пользователей (/start): {total_users}\n"
        f"🆕 Новых сегодня: {new_today}\n"
        f"▶️ Запускали /start сегодня: {started_today}\n\n"
        f"📈 DAU (генерации сегодня): {dau_users}\n"
        f"⚙️ Генераций сегодня (total): {runs_today}\n"
        f"📅 MAU (за 30 дней): {mau}\n"
        f"🗓️ Генераций за 7 дней (total): {runs_7d}\n"
    )
    await message.answer(text)

@router.message(Command("stats"))
async def admin_stats_cmd(message: Message, bot: Bot):
    if not is_admin(message.from_user.id):
        return
    await admin_stats_button(message, bot)

# ================== ADMIN: BROADCAST ==================
@router.message(F.text == "📣 Рассылка")
async def admin_broadcast_button(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id):
        return

    await safe_delete(bot, message.chat.id, message.message_id)

    await state.set_state(AdminFlow.broadcast_wait_text)
    await message.answer(
        "📣 Рассылка\n\n"
        "Пришли ОДНИМ сообщением текст рассылки.\n"
        "Чтобы отменить — /cancel"
    )

@router.message(Command("cancel"))
async def cancel_any(message: Message, state: FSMContext):
    if is_admin(message.from_user.id):
        await state.clear()
        await message.answer("Ок, отменил.")

@router.message(AdminFlow.broadcast_wait_text)
async def admin_broadcast_send(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    text = (message.text or "").strip()
    if not text:
        await message.answer("Пришли текст рассылки (обычный текст).")
        return

    await state.clear()

    users = await load_users()
    user_ids = [int(uid) for uid in users.keys()]

    if not user_ids:
        await message.answer("Пока нет пользователей для рассылки (никто не нажал /start).")
        return

    await message.answer(f"Запускаю рассылку на {len(user_ids)} пользователей…")

    ok = 0
    fail = 0

    for uid in user_ids:
        try:
            await bot.send_message(uid, text)
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)  # антифлуд

    await message.answer(f"✅ Рассылка завершена.\nУспешно: {ok}\nОшибок: {fail}")

# ================== OPTIONAL: /stop (opt-out) ==================
@router.message(Command("stop"))
async def stop_cmd(message: Message):
    await remove_user(message.from_user.id)
    await message.answer("Ок. Я больше не буду писать тебе рассылки. Чтобы вернуться — нажми /start.")

# ================== MAIN ==================
async def main():
    global openai_client

    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN missing in .env")

    if not OPENAI_API_KEY:
        print("WARNING: OPENAI_API_KEY is missing. Ideas generation will fail.")

    openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else AsyncOpenAI(api_key="")

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    print("Bot started.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())