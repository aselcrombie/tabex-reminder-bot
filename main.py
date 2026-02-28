"""
TabexReminder — Telegram bot for Tabex course reminders.
Long polling, single process, background scheduler. Data in data.json.
"""

import asyncio
import json
import logging
import os
import re
import threading

from dotenv import load_dotenv

load_dotenv()
from datetime import datetime, timedelta
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

# --- Config ------------------------------------------------------------------
TOKEN = os.getenv("TELEGRAM_TOKEN")
DATA_PATH = Path("data.json")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("tabex")

# --- Tabex schedule (official: tabex.kz) -------------------------------------
# Days 1–3: 1 tablet every 2 h → 6/day
# Days 4–12: 1 tablet every 2.5 h → 5/day
# Days 13–16: 1 tablet every 3 h → 4/day
# Days 17–20: 1 tablet every 5 h → 3/day
# Days 21–25: 1–2 tablets/day → 2 doses, 12 h apart
def get_required_doses(day: int) -> int:
    if day < 1 or day > 25:
        return 0
    if day <= 3:
        return 6
    if day <= 12:
        return 5
    if day <= 16:
        return 4
    if day <= 20:
        return 3
    return 2


def get_interval_hours(day: int) -> float:
    """Hours between doses for this day. Next reminder = last_dose_time + this."""
    if day < 1 or day > 25:
        return 2.0
    if day <= 3:
        return 2.0
    if day <= 12:
        return 2.5
    if day <= 16:
        return 3.0
    if day <= 20:
        return 5.0
    return 12.0  # 21–25: 2 doses per day


def get_interval_description(day: int) -> str:
    """Human-readable interval for UI (час/часа/часов)."""
    h = get_interval_hours(day)
    if h == int(h):
        n = int(h)
        if n == 1:
            word = "час"
        elif 2 <= n <= 4:
            word = "часа"
        else:
            word = "часов"
        return f"каждые {n} {word}"
    # 2.5
    return "каждые 2,5 часа"


# --- Data (lock + atomic write to avoid scheduler/handler race) ---------------
_data_lock = threading.Lock()


def load_data() -> dict:
    with _data_lock:
        if not DATA_PATH.exists():
            return {"users": {}}
        try:
            with open(DATA_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.exception("Failed to load data.json: %s", e)
            return {"users": {}}


def save_data(data: dict) -> None:
    tmp_path = DATA_PATH.with_suffix(".json.tmp")
    with _data_lock:
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, DATA_PATH)
        except Exception as e:
            logger.exception("Failed to save data.json: %s", e)
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass


# In-memory state for users during onboarding (start date / timezone)
pending_onboarding: dict[str, dict] = {}


def _migrate_user_reminders(u: dict) -> bool:
    """Replace legacy pendingReminders with nextReminderTimestamp. Returns True if migration was done."""
    u.setdefault("nextReminderTimestamp", None)
    u.setdefault("postponedReminderTimestamp", None)
    if "pendingReminders" not in u or not u["pendingReminders"]:
        if "pendingReminders" in u:
            del u["pendingReminders"]
            return True
        return False
    # Не перезаписывать уже установленный nextReminderTimestamp (сохраняем при перезапуске)
    if u.get("nextReminderTimestamp"):
        del u["pendingReminders"]
        return True
    pending = u["pendingReminders"]
    earliest = None
    for pr in pending:
        t = pr.get("triggerAt")
        if not t:
            continue
        try:
            dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
            if earliest is None or dt < earliest:
                earliest = dt
        except Exception:
            continue
    if earliest is not None:
        u["nextReminderTimestamp"] = earliest.isoformat().replace("+00:00", "Z")
    u["postponedReminderTimestamp"] = None
    del u["pendingReminders"]
    return True


def parse_timezone(s: str) -> int | None:
    """Parse timezone string like '+5', '-7', '+03' to offset hours. None if invalid."""
    s = s.strip()
    m = re.match(r"^([+-]?\d{1,2})(?::(\d{2}))?$", s)
    if not m:
        return None
    h = int(m.group(1))
    if m.group(2):
        return None  # we only support whole hours for simplicity
    if h < -12 or h > 14:
        return None
    return h


def parse_date(s: str) -> str | None:
    """Return YYYY-MM-DD if valid, else None."""
    s = s.strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return None
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return s
    except ValueError:
        return None


def user_local_now(offset_hours: int) -> datetime:
    """Current datetime in user's timezone (offset from UTC in hours)."""
    return datetime.utcnow() + timedelta(hours=offset_hours)


def user_local_time_str(offset_hours: int) -> str:
    """Current time as HH:MM in user's timezone."""
    t = user_local_now(offset_hours)
    return t.strftime("%H:%M")


def user_local_date_str(offset_hours: int) -> str:
    """Current date as YYYY-MM-DD in user's timezone."""
    t = user_local_now(offset_hours)
    return t.strftime("%Y-%m-%d")


def get_user_current_day(user: dict) -> int:
    """Compute current day of course from startDate and today (user timezone)."""
    start = user["startDate"]
    tz = int(user["timezone"])
    today = user_local_date_str(tz)
    if today < start:
        return 0
    start_d = datetime.strptime(start, "%Y-%m-%d")
    today_d = datetime.strptime(today, "%Y-%m-%d")
    delta = (today_d - start_d).days
    return delta + 1


# --- Bot ---------------------------------------------------------------------
bot = Bot(token=TOKEN)
dp = Dispatcher()
router = Router(name="tabex")


# Disclaimer: bot is not a doctor, not medical advisor, just a reminder
DISCLAIMER = "⚠️ Бот не является врачом и не даёт медицинских рекомендаций."

TABEX_INSTRUCTION_URL = "https://tabex.kz/"


def format_date_dd_mm_yyyy(iso_date: str) -> str:
    """Format YYYY-MM-DD as DD-MM-YYYY for display."""
    d = datetime.strptime(iso_date, "%Y-%m-%d")
    return d.strftime("%d-%m-%Y")


@router.message(Command("start"))
async def cmd_start(msg: Message) -> None:
    uid = str(msg.from_user.id)
    data = load_data()
    users = data["users"]

    if uid in users:
        u = users[uid]
        if u.get("courseCompleted"):
            await msg.answer(
                "Вы уже прошли курс. Бот является напоминалкой и не заменяет консультацию врача."
            )
            return
        day = u.get("currentDay", 1)
        if day > 25:
            await msg.answer("Курс завершён. Спасибо, что пользовались ботом.")
            return
        required = get_required_doses(day)
        interval_desc = get_interval_description(day)
        await msg.answer(
            f"Вы уже зарегистрированы.\n"
            f"Сегодня день {day}. Нужно принять {required} таблеток ({interval_desc}).\n\n{DISCLAIMER}"
        )
        return

    # Start onboarding: first ask timezone, then confirm today's date
    pending_onboarding[uid] = {"step": "timezone"}
    await msg.answer(
        "Добро пожаловать в Tabex Reminder.\n\n"
        "Введите ваш часовой пояс (например +5 для UTC+5 или -7 для UTC-7):"
    )


def _save_new_user(uid: str, start_date: str, tz: int) -> None:
    data = load_data()
    data["users"][uid] = {
        "startDate": start_date,
        "timezone": str(tz),
        "currentDay": 1,
        "takenToday": 0,
        "lastDoseTimestamp": None,
        "courseCompleted": False,
        "lastMorningMessageDate": None,
        "nextReminderTimestamp": None,
        "postponedReminderTimestamp": None,
    }
    save_data(data)


def build_first_day_message(start_date_iso: str) -> str:
    date_display = format_date_dd_mm_yyyy(start_date_iso)
    return (
        f"Отлично! Сегодня ваш первый день приёма Табекс ({date_display}).\n\n"
        "Давайте отметим это, приняв первую таблетку по схеме. "
        "Как только будет готово — нажмите кнопку «Готово», и я напомню о следующем приёме через два часа.\n\n"
        f"📋 Ознакомьтесь с инструкцией и способом применения: {TABEX_INSTRUCTION_URL}\n"
        "Нажимая «Готово», вы подтверждаете, что ознакомились с инструкцией и противопоказаниями.\n\n"
        f"{DISCLAIMER}"
    )


@router.message(F.text)
async def on_text(msg: Message) -> None:
    uid = str(msg.from_user.id)
    text = (msg.text or "").strip()

    # Onboarding: waiting for timezone
    if uid in pending_onboarding and pending_onboarding[uid].get("step") == "timezone":
        tz = parse_timezone(text)
        if tz is None:
            await msg.answer("Неверный формат. Введите число часов от UTC, например +5 или -7:")
            return
        today_iso = user_local_date_str(tz)
        date_display = format_date_dd_mm_yyyy(today_iso)
        pending_onboarding[uid]["step"] = "confirm_date"
        pending_onboarding[uid]["timezone"] = tz
        pending_onboarding[uid]["today_iso"] = today_iso
        keyb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="Подтвердить", callback_data="start_confirm_yes"),
                InlineKeyboardButton(text="Другая дата", callback_data="start_confirm_no"),
            ]
        ])
        await msg.answer(
            f"Подтвердите: дата начала приёма Табекс — сегодня {date_display}?\n\n"
            f"📋 Инструкция: {TABEX_INSTRUCTION_URL}",
            reply_markup=keyb,
        )
        return

    # Onboarding: optional date (user chose "Другая дата")
    if uid in pending_onboarding and pending_onboarding[uid].get("step") == "optional_date":
        start_date = parse_date(text)
        if not start_date:
            await msg.answer("Неверный формат. Введите дату в формате ГГГГ-ММ-ДД (например 2025-03-01):")
            return
        tz = pending_onboarding[uid]["timezone"]
        _save_new_user(uid, start_date, tz)
        del pending_onboarding[uid]
        keyb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Готово", callback_data="first_ready")],
        ])
        await msg.answer(
            build_first_day_message(start_date),
            reply_markup=keyb,
        )
        return

    # Free-text "Принял": выпил, выпила, принял, таблетка, etc.
    data = load_data()
    if uid in data["users"] and uid not in pending_onboarding:
        u = data["users"][uid]
        _migrate_user_reminders(u)
        if not u.get("courseCompleted") and any(phrase in text.lower() for phrase in TAKEN_PHRASES):
            now = datetime.utcnow()
            completion = _apply_taken(u, now)
            save_data(data)
            await msg.answer("✓ Учтено.")
            if completion:
                await msg.answer(completion)
            return


# Inline callback names (two buttons only)
CB_TAKEN = "taken"
CB_POSTPONE = "postpone"
CB_MISSED_YES = "missed_yes"
CB_MISSED_NO = "missed_no"

# Free-text phrases that count as "Принял"
TAKEN_PHRASES = frozenset(s.lower() for s in (
    "выпил", "выпила", "выпил таблетку", "принял", "таблетка",
))


def dose_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Принял", callback_data=CB_TAKEN),
            InlineKeyboardButton(text="Напомнить позже", callback_data=CB_POSTPONE),
        ],
    ])


def _apply_taken(u: dict, now: datetime) -> str | None:
    """
    Apply "Принял" logic to user dict (mutates u).
    Clears nextReminderTimestamp and postponedReminderTimestamp.
    Returns completion message if course just finished, else None.
    """
    u["takenToday"] = u.get("takenToday", 0) + 1
    u["lastDoseTimestamp"] = now.isoformat() + "Z"
    u["nextReminderTimestamp"] = None
    u["postponedReminderTimestamp"] = None
    day = u.get("currentDay", 1)
    required = get_required_doses(day)
    if u["takenToday"] >= required:
        u["currentDay"] = day + 1
        u["takenToday"] = 0
        u["lastMorningMessageDate"] = None
    next_day = u.get("currentDay", 1)
    if next_day > 25:
        u["courseCompleted"] = True
        return (
            "Поздравляем! Вы завершили курс приёма Табекс по схеме 25 дней. "
            "Бот является напоминалкой и не заменяет консультацию врача."
        )
    if next_day <= 25:
        interval_h = get_interval_hours(next_day)
        u["nextReminderTimestamp"] = (now + timedelta(hours=interval_h)).isoformat() + "Z"
    return None


@router.callback_query(F.data == CB_TAKEN)
async def cb_taken(cq: CallbackQuery) -> None:
    await cq.answer()
    uid = str(cq.from_user.id)
    data = load_data()
    if uid not in data["users"]:
        return
    u = data["users"][uid]
    _migrate_user_reminders(u)
    if u.get("courseCompleted"):
        return
    now = datetime.utcnow()
    completion = _apply_taken(u, now)
    save_data(data)
    await cq.message.edit_text((cq.message.text or "Приём") + "\n\n✓ Учтено.")
    if completion:
        await cq.message.answer(completion)


@router.callback_query(F.data == CB_POSTPONE)
async def cb_postpone(cq: CallbackQuery) -> None:
    await cq.answer("Напоминание через 15 минут")
    uid = str(cq.from_user.id)
    data = load_data()
    if uid not in data["users"]:
        return
    u = data["users"][uid]
    if u.get("courseCompleted"):
        return
    trigger_at = (datetime.utcnow() + timedelta(minutes=15)).isoformat() + "Z"
    u["postponedReminderTimestamp"] = trigger_at
    save_data(data)
    await cq.message.edit_text((cq.message.text or "Приём") + "\n\nНапомню через 15 минут.")


@router.callback_query(F.data.startswith(CB_MISSED_YES))
async def cb_missed_yes(cq: CallbackQuery) -> None:
    await cq.answer()
    uid = str(cq.from_user.id)
    data = load_data()
    if uid not in data["users"]:
        return
    u = data["users"][uid]
    _migrate_user_reminders(u)
    u["nextReminderTimestamp"] = None
    u["postponedReminderTimestamp"] = None
    day = u.get("currentDay", 1)
    required = get_required_doses(day)
    missed = required - u.get("takenToday", 0)
    u["takenToday"] = u.get("takenToday", 0) + missed
    if u["takenToday"] >= required:
        u["currentDay"] = day + 1
        u["takenToday"] = 0
        u["lastMorningMessageDate"] = None
    save_data(data)
    await cq.message.edit_text((cq.message.text or "") + "\n\nПриёмы отмечены выполненными.")


@router.callback_query(F.data.startswith(CB_MISSED_NO))
async def cb_missed_no(cq: CallbackQuery) -> None:
    await cq.answer()
    await cq.message.edit_text((cq.message.text or "") + "\n\nХорошо.")


# --- Onboarding callbacks: confirm start date, first dose "Готово" ---
@router.callback_query(F.data == "start_confirm_yes")
async def cb_start_confirm_yes(cq: CallbackQuery) -> None:
    await cq.answer()
    uid = str(cq.from_user.id)
    if uid not in pending_onboarding or pending_onboarding[uid].get("step") != "confirm_date":
        await cq.message.edit_text("Сессия устарела. Отправьте /start заново.")
        return
    today_iso = pending_onboarding[uid]["today_iso"]
    tz = pending_onboarding[uid]["timezone"]
    _save_new_user(uid, today_iso, tz)
    del pending_onboarding[uid]
    keyb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Готово", callback_data="first_ready")],
    ])
    await cq.message.edit_reply_markup(reply_markup=None)
    await cq.message.answer(
        build_first_day_message(today_iso),
        reply_markup=keyb,
    )


@router.callback_query(F.data == "start_confirm_no")
async def cb_start_confirm_no(cq: CallbackQuery) -> None:
    await cq.answer()
    uid = str(cq.from_user.id)
    if uid not in pending_onboarding or pending_onboarding[uid].get("step") != "confirm_date":
        await cq.message.edit_text("Сессия устарела. Отправьте /start заново.")
        return
    pending_onboarding[uid]["step"] = "optional_date"
    await cq.message.edit_reply_markup(reply_markup=None)
    await cq.message.answer(
        "Введите дату начала приёма в формате ГГГГ-ММ-ДД (например 2025-03-01):"
    )


@router.callback_query(F.data == "first_ready")
async def cb_first_ready(cq: CallbackQuery) -> None:
    uid = str(cq.from_user.id)
    data = load_data()
    if uid not in data["users"]:
        return
    u = data["users"][uid]
    _migrate_user_reminders(u)
    if u.get("courseCompleted"):
        return
    day = u.get("currentDay", 1)
    interval_h = get_interval_hours(day)
    await cq.answer(f"Принято. Напоминание через {interval_h} ч.")
    now = datetime.utcnow()
    completion = _apply_taken(u, now)
    save_data(data)
    await cq.message.edit_text(
        (cq.message.text or "") + f"\n\n✓ Приём учтён. Следующее напоминание — через {interval_h} ч."
    )
    if completion:
        await cq.message.answer(completion)


dp.include_router(router)


# --- Scheduler ---------------------------------------------------------------
async def run_scheduler() -> None:
    """Every 60 seconds: morning message, dose reminders, 21:00 check, pending reminders."""
    while True:
        try:
            await tick()
        except Exception as e:
            logger.exception("Scheduler tick error: %s", e)
        await asyncio.sleep(60)


async def tick() -> None:
    data = load_data()
    users = data["users"]
    now_utc = datetime.utcnow()

    for uid, u in list(users.items()):
        if u.get("courseCompleted"):
            continue
        try:
            tz = int(u.get("timezone", 0))
        except (ValueError, TypeError):
            continue
        today_user = user_local_date_str(tz)
        time_user = user_local_time_str(tz)
        day = u.get("currentDay", 1)
        if day > 25:
            # Course completed
            u["courseCompleted"] = True
            save_data(data)
            try:
                await bot.send_message(
                    uid,
                    "Поздравляем! Вы завершили курс приёма Табекс по схеме 25 дней. "
                    "Бот является напоминалкой и не заменяет консультацию врача."
                )
            except Exception as e:
                logger.warning("Failed to send completion message to %s: %s", uid, e)
            continue

        required = get_required_doses(day)
        interval_desc = get_interval_description(day)

        # 1) Morning summary at 08:00, once per day — "примите первую таблетку, нажмите Готово"
        if time_user >= "07:59" and time_user <= "08:01":
            if u.get("lastMorningMessageDate") != today_user:
                u["lastMorningMessageDate"] = today_user
                save_data(data)
                keyb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="Готово", callback_data="first_ready")],
                ])
                try:
                    await bot.send_message(
                        uid,
                        f"Доброе утро! Сегодня {day}-й день приёма Табекс.\n"
                        f"Сегодня нужно принять {required} таблеток ({interval_desc}).\n"
                        "Примите первую таблетку и нажмите «Готово» — следующее напоминание придёт через нужный интервал.",
                        reply_markup=keyb,
                    )
                except Exception as e:
                    logger.warning("Morning message to %s failed: %s", uid, e)

        # 2) 21:00 check: missed doses
        if time_user >= "20:59" and time_user <= "21:02":
            missed = required - u.get("takenToday", 0)
            check_key = "last21Check"
            if missed > 0 and u.get(check_key) != today_user:
                u[check_key] = today_user
                save_data(data)
                keyb = InlineKeyboardMarkup(inline_keyboard=[
                    [
                        InlineKeyboardButton(text="Да", callback_data=CB_MISSED_YES),
                        InlineKeyboardButton(text="Нет", callback_data=CB_MISSED_NO),
                    ]
                ])
                try:
                    await bot.send_message(
                        uid,
                        f"Вы пропустили {missed} приём(ов). Хотите выполнить их сейчас?",
                        reply_markup=keyb,
                    )
                except Exception as e:
                    logger.warning("21:00 check message to %s failed: %s", uid, e)

        # 3) nextReminderTimestamp or postponedReminderTimestamp due → send reminder, then clear
        need_save = _migrate_user_reminders(u)
        next_ts = u.get("nextReminderTimestamp")
        post_ts = u.get("postponedReminderTimestamp")
        if next_ts:
            try:
                next_dt = datetime.fromisoformat(next_ts.replace("Z", "+00:00"))
                if now_utc >= next_dt:
                    await bot.send_message(
                        uid,
                        f"Напоминание: приём Табекс ({day}-й день).",
                        reply_markup=dose_keyboard(),
                    )
                    u["nextReminderTimestamp"] = None
                    need_save = True
            except Exception as e:
                logger.warning("nextReminderTimestamp parse/send for %s: %s", uid, e)
                u["nextReminderTimestamp"] = None
                need_save = True
        if post_ts:
            try:
                post_dt = datetime.fromisoformat(post_ts.replace("Z", "+00:00"))
                if now_utc >= post_dt:
                    await bot.send_message(
                        uid,
                        f"Напоминание: приём Табекс ({day}-й день).",
                        reply_markup=dose_keyboard(),
                    )
                    u["postponedReminderTimestamp"] = None
                    need_save = True
            except Exception as e:
                logger.warning("postponedReminderTimestamp parse/send for %s: %s", uid, e)
                u["postponedReminderTimestamp"] = None
                need_save = True
        if need_save:
            save_data(data)


# --- Main --------------------------------------------------------------------
async def main() -> None:
    if not TOKEN:
        logger.error("TELEGRAM_TOKEN is not set")
        raise SystemExit(1)
    logger.info("Starting TabexReminder (long polling + scheduler)")
    dp["scheduler_task"] = asyncio.create_task(run_scheduler())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
