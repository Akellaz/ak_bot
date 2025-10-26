# main.py
import os
import asyncio
from datetime import date, datetime
from typing import List
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
import asyncpg
from aiogram import Bot, Dispatcher
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram_dialog import Dialog, DialogManager, Window, setup_dialogs, StartMode
from aiogram_dialog.widgets.kbd import (
    Calendar,
    Multiselect,
    Button,
    Radio,
    ScrollingGroup,
)
from aiogram_dialog.widgets.text import Const, Format, Jinja
from aiogram_dialog.widgets.kbd.calendar_kbd import (
    DATE_TEXT,
    TODAY_TEXT,
    CalendarDaysView,
    CalendarMonthView,
    CalendarYearsView,
    CalendarScope,
    CalendarConfig,
)
from aiogram.filters.state import StatesGroup, State
from babel.dates import get_day_names
import operator

# ============= CONFIG =============
WEBHOOK_PATH = "/webhook"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "your-secret-here")
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is required")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL is required")

WEB_SERVER_HOST = "0.0.0.0"
WEB_SERVER_PORT = int(os.getenv("PORT", 8000))
BASE_WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL", "https://your-render-url.onrender.com").rstrip()

# ============= DB INIT =============
async def init_db():
    conn = await asyncpg.connect(DATABASE_URL)

    # Студии
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS venues (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            address TEXT NOT NULL,
            city TEXT DEFAULT 'Москва',
            contact TEXT,
            hourly_rate INTEGER,
            is_active BOOLEAN DEFAULT true
        )
    """)

    # Преподаватели
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS teachers (
            id SERIAL PRIMARY KEY,
            tg_user_id BIGINT UNIQUE,
            name TEXT NOT NULL,
            instrument TEXT NOT NULL,
            city TEXT DEFAULT 'Москва',
            price_per_hour INTEGER,
            description TEXT,
            venue_id INTEGER REFERENCES venues(id),
            location_type TEXT DEFAULT 'studio',
            is_active BOOLEAN DEFAULT true
        )
    """)

    # Бронирования
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS book (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            date DATE NOT NULL,
            time TEXT NOT NULL,
            author TEXT NOT NULL,
            teacher_id INTEGER REFERENCES teachers(id),
            venue_id INTEGER REFERENCES venues(id),
            price INTEGER
        )
    """)

    # Уникальность по преподавателю
    await conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_teacher_booking 
        ON book (teacher_id, date, time)
    """)

    await conn.close()

# ============= CALENDAR WIDGETS =============
SELECTED_DAYS_KEY = "selected_dates"

class WeekDay:
    async def _render_text(self, data, manager: DialogManager) -> str:
        selected_date: date = data["date"]
        locale = manager.event.from_user.language_code or "en"
        return get_day_names(width="short", context="stand-alone", locale=locale)[selected_date.weekday()].title()

class MarkedDay:
    def __init__(self, mark: str, other):
        self.mark = mark
        self.other = other

    async def _render_text(self, data, manager: DialogManager) -> str:
        current_date: date = data["date"]
        serial_date = current_date.isoformat()
        selected = manager.dialog_data.get(SELECTED_DAYS_KEY, [])
        if serial_date in selected:
            return self.mark
        return await self.other._render_text(data, manager)

class CustomCalendar:
    def __init__(self):
        pass

    def _init_views(self) -> dict:
        config = CalendarConfig()
        return {
            CalendarScope.DAYS: CalendarDaysView(
                self._item_callback_data,
                config=config,
                date_text=MarkedDay("🔴", DATE_TEXT),
                today_text=MarkedDay("⭕", TODAY_TEXT),
                header_text=Format("~~~~~ {date:%B} ~~~~~"),
                weekday_text=WeekDay(),
                next_month_text=Format("{date:%B} >>"),
                prev_month_text=Format("<< {date:%B}"),
            ),
            CalendarScope.MONTHS: CalendarMonthView(
                self._item_callback_data,
                config=config,
                month_text=Format("{date:%B}"),
                header_text=Format("~~~~~ {date:%Y} ~~~~~"),
                this_month_text=Format("[{date:%B}]"),
            ),
            CalendarScope.YEARS: CalendarYearsView(
                self._item_callback_data,
                config=config,
            ),
        }

    def _item_callback_data(self, data: str) -> str:
        return data

# ============= STATES =============
class BookingStates(StatesGroup):
    select_instrument = State()
    select_teacher = State()
    select_date = State()
    select_time = State()
    confirm = State()

# ============= GETTERS & CALLBACKS =============
async def get_instruments(**kwargs):
    return {
        "instruments": [("Ударные", "drums")]
        # Добавишь позже: ("Вокал", "vocal"), ("Гитара", "guitar")
    }

async def on_instrument_selected(callback: CallbackQuery, widget, manager: DialogManager, item_id: str):
    manager.dialog_data["instrument"] = item_id
    await manager.next()

async def get_teachers(dialog_manager: DialogManager, **kwargs):
    instrument = dialog_manager.dialog_data["instrument"]
    conn = await asyncpg.connect(DATABASE_URL)
    rows = await conn.fetch("""
        SELECT id, name, price_per_hour, description 
        FROM teachers 
        WHERE instrument = $1 AND is_active = true AND city = 'Москва'
    """, instrument)
    await conn.close()
    teachers = [(f"{r['name']} — {r['price_per_hour']} ₽/час\n{r['description']}", r['id']) for r in rows]
    return {"teachers": teachers}

async def on_teacher_selected(callback: CallbackQuery, widget, manager: DialogManager, teacher_id: str):
    manager.dialog_data["teacher_id"] = int(teacher_id)
    await manager.next()

async def win1_on_date_selected(callback: CallbackQuery, widget, manager: DialogManager, selected_date: date):
    manager.dialog_data["selected_date"] = selected_date.isoformat()
    await manager.next()

async def get_time(dialog_manager: DialogManager, **kwargs):
    selected_date_str = dialog_manager.dialog_data.get("selected_date")
    teacher_id = dialog_manager.dialog_data.get("teacher_id")
    if not selected_date_str or not teacher_id:
        return {"time_slots": [], "time_slots2": [], "count": 0, "count2": 0}

    try:
        selected_date = date.fromisoformat(selected_date_str)
    except ValueError:
        return {"time_slots": [], "time_slots2": [], "count": 0, "count2": 0}

    conn = await asyncpg.connect(DATABASE_URL)
    rows = await conn.fetch(
        "SELECT time FROM book WHERE date = $1 AND teacher_id = $2",
        selected_date, teacher_id
    )
    booked_times = {row["time"] for row in rows}
    await conn.close()

    time_slots_zero1 = ["8:00", "9:00", "10:00", "11:00", "12:00", "13:00", "14:00", "15:00"]
    time_slots_zero2 = ["16:00", "17:00", "18:00", "19:00", "20:00", "21:00", "22:00", "23:00"]

    time_slots = [(t, t) for t in time_slots_zero1 if t not in booked_times]
    time_slots2 = [(t, t) for t in time_slots_zero2 if t not in booked_times]

    return {
        "time_slots": time_slots,
        "time_slots2": time_slots2,
        "count": len(time_slots),
        "count2": len(time_slots2),
    }

async def on_book_click(callback: CallbackQuery, button, manager: DialogManager):
    from asyncpg import UniqueViolationError

    selected_date_str = manager.dialog_data.get("selected_date")
    teacher_id = manager.dialog_data.get("teacher_id")
    if not selected_date_str or not teacher_id:
        await callback.answer("❌ Ошибка: не хватает данных", show_alert=True)
        return

    try:
        selected_date = date.fromisoformat(selected_date_str)
    except ValueError:
        await callback.answer("❌ Ошибка даты!", show_alert=True)
        return

    m1 = manager.find("m_time_slots")
    m2 = manager.find("m_time_slots2")
    checked1 = m1.get_checked() if m1 else []
    checked2 = m2.get_checked() if m2 else []
    checked = list(checked1) + list(checked2)

    if not checked:
        await callback.answer("❌ Выбери время!", show_alert=True)
        return

    author = callback.from_user.username or f"user_{callback.from_user.id}"
    name = author

    conn = await asyncpg.connect(DATABASE_URL)
    try:
        # Получаем данные препода
        teacher = await conn.fetchrow(
            "SELECT venue_id, price_per_hour FROM teachers WHERE id = $1", teacher_id
        )
        if not teacher:
            raise ValueError("Преподаватель не найден")

        venue_id = teacher["venue_id"]
        price = teacher["price_per_hour"]

        for t in checked:
            await conn.execute("""
                INSERT INTO book (name, date, time, author, teacher_id, venue_id, price)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
            """, name, selected_date, t, author, teacher_id, venue_id, price)

        manager.dialog_data.update({
            "final_date": selected_date.isoformat(),
            "final_times": checked,
            "final_author": author,
            "final_teacher_id": teacher_id,
            "final_price": price,
        })
        await manager.next()

    except UniqueViolationError:
        await callback.answer("⚠️ Слот уже занят! Выбери другое время.", show_alert=True)
    except Exception as e:
        await callback.answer(f"❌ Ошибка: {str(e)}", show_alert=True)
    finally:
        await conn.close()

async def final_getter(dialog_manager: DialogManager, **kwargs):
    data = dialog_manager.dialog_data
    return {
        "date": data.get("final_date", "—"),
        "author_user": data.get("final_author", "—"),
        "times": ", ".join(data.get("final_times", [])) or "—",
        "price": data.get("final_price", 0),
    }

# ============= DIALOG =============
dialog = Dialog(
    Window(
        Const("Выбери инструмент:"),
        Radio(
            Format("✓ {item[0]}"),
            Format("{item[0]}"),
            id="instr",
            item_id_getter=lambda x: x[1],
            items="instruments",
            on_click=on_instrument_selected,
        ),
        state=BookingStates.select_instrument,
        getter=get_instruments,
    ),
    Window(
        Const("Выбери преподавателя:"),
        ScrollingGroup(
            Radio(
                Format("✓ {item[0]}"),
                Format("{item[0]}"),
                id="teacher_radio",
                item_id_getter=lambda x: x[1],
                items="teachers",
                on_click=on_teacher_selected,
            ),
            width=1,
            height=5,
            id="teacher_scroll",
        ),
        state=BookingStates.select_teacher,
        getter=get_teachers,
    ),
    Window(
        Const("Выбери дату:"),
        CustomCalendar(),
        state=BookingStates.select_date,
        on_click=win1_on_date_selected,
    ),
    Window(
        Const("Выбери время (можно несколько):"),
        CustomCalendar(),
        Multiselect(
            Format("✓ {item[0]}"),
            Format("{item[0]}"),
            id="m_time_slots",
            item_id_getter=operator.itemgetter(1),
            items="time_slots",
        ),
        Multiselect(
            Format("✓ {item[0]}"),
            Format("{item[0]}"),
            id="m_time_slots2",
            item_id_getter=operator.itemgetter(1),
            items="time_slots2",
        ),
        Button(Const("Забить"), id="book_btn", on_click=on_book_click),
        getter=get_time,
        state=BookingStates.select_time,
    ),
    Window(
        Const("✅ Время забронировано"),
        Jinja(
            "<b>Дата</b>: {{date}}\n"
            "<b>Время</b>: {{times}}\n"
            "<b>Стоимость</b>: {{price}} ₽\n"
            "<b>Автор</b>: {{author_user}}\n"
        ),
        state=BookingStates.confirm,
        getter=final_getter,
        parse_mode="html",
    ),
)

# ============= FASTAPI + BOT =============
app = FastAPI()
storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=storage)

dp.include_router(dialog)
setup_dialogs(dp)

@app.on_event("startup")
async def on_startup():
    await init_db()
    webhook_url = f"{BASE_WEBHOOK_URL}{WEBHOOK_PATH}"
    await bot.set_webhook(
        url=webhook_url,
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=True
    )

@app.post(WEBHOOK_PATH)
async def bot_webhook(request: Request):
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if secret != WEBHOOK_SECRET:
        return {"error": "Invalid secret"}
    update = await request.json()
    await dp.feed_raw_update(bot, update)
    return {"status": "ok"}

# ============= DASHBOARD =============
@app.get("/dashboard")
async def dashboard(request: Request):
    from collections import defaultdict

    teacher_id = request.query_params.get("teacher_id")
    date_from_str = request.query_params.get("date_from")
    date_to_str = request.query_params.get("date_to")

    conn = await asyncpg.connect(DATABASE_URL)

    # Формируем запрос
    query = """
        SELECT b.date, b.time, b.author, b.id, t.name AS teacher_name, v.name AS venue_name
        FROM book b
        LEFT JOIN teachers t ON b.teacher_id = t.id
        LEFT JOIN venues v ON b.venue_id = v.id
    """
    conditions = []
    params = []
    param_index = 1

    if teacher_id:
        conditions.append(f"b.teacher_id = ${param_index}")
        params.append(int(teacher_id))
        param_index += 1

    if date_from_str:
        try:
            date_from = date.fromisoformat(date_from_str)
            conditions.append(f"b.date >= ${param_index}")
            params.append(date_from)
            param_index += 1
        except:
            pass

    if date_to_str:
        try:
            date_to = date.fromisoformat(date_to_str)
            conditions.append(f"b.date <= ${param_index}")
            params.append(date_to)
            param_index += 1
        except:
            pass

    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY b.date, b.time"

    rows = await conn.fetch(query, *params)
    await conn.close()

    # Группировка по датам
    bookings_by_date = defaultdict(list)
    for r in rows:
        bookings_by_date[r['date']].append(r)

    # Подсчёт
    total = len(rows)
    today = date.today()
    today_count = len([r for r in rows if r['date'] == today])

    # HTML
    html = f"""<!DOCTYPE html>
    <html><head><meta charset="utf-8"><title>Бронирования</title></head><body>
    <h2>Бронирования {'для преподавателя' if teacher_id else 'все'}</h2>
    <p>Всего: {total} | Сегодня: {today_count}</p>
    <a href="/dashboard">Все</a> | 
    <a href="/dashboard?teacher_id=1">Препод 1</a>
    <hr>
    """
    if not rows:
        html += "<p>Нет бронирований</p>"
    else:
        for d in sorted(bookings_by_date):
            html += f"<h3>📅 {d}</h3><ul>"
            for b in bookings_by_date[d]:
                venue = b['venue_name'] or 'Дом'
                html += f"<li>⏰ {b['time']} — @{b['author']} — {venue}</li>"
            html += "</ul>"
    html += "</body></html>"
    return HTMLResponse(html)

@app.get("/")
async def root():
    return {"status": "OK", "dashboard": "/dashboard"}

@dp.message(Command("start"))
async def start(message: Message, dialog_manager: DialogManager):
    await dialog_manager.start(BookingStates.select_instrument, mode=StartMode.RESET_STACK)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=WEB_SERVER_PORT)