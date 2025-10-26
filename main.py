# main.py
import os
import asyncio
from datetime import date
from typing import List
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
import asyncpg
from aiogram import Bot, Dispatcher
from aiogram.types import Message
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram_dialog import Dialog, DialogManager, Window, setup_dialogs, StartMode
from aiogram_dialog.widgets.kbd import (
    Calendar,
    Multiselect,
    Next,
    Back,
    Button,
)
from aiogram_dialog.widgets.text import Const, Format, Jinja
from aiogram_dialog.widgets.kbd.calendar_kbd import (
    DATE_TEXT,
    TODAY_TEXT,
    CalendarDaysView,
    CalendarMonthView,
    CalendarYearsView,
    CalendarScope,
    CalendarScopeView,
    CalendarConfig,
)
from aiogram.filters.state import StatesGroup, State
from aiogram.types import CallbackQuery
from aiogram import F
from babel.dates import get_day_names, get_month_names
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
BASE_WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL", f"https://your-render-url.onrender.com")

# ============= DB =============
async def init_db():
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS book (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            date DATE NOT NULL,
            time TEXT NOT NULL,
            author TEXT NOT NULL
        )
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

class CustomCalendar(Calendar):
    def _init_views(self) -> dict[CalendarScope, CalendarScopeView]:
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

# ============= STATES & GLOBAL =============
class MySG(StatesGroup):
    window1 = State()
    window2 = State()
    window3 = State()

# ============= DIALOG CALLBACKS =============
async def win1_on_date_selected(callback: CallbackQuery, widget, manager: DialogManager, selected_date: date):
    manager.dialog_data["selected_date"] = selected_date.isoformat()
    await manager.next()

async def get_time(dialog_manager: DialogManager, event_from_user, **kwargs):
    selected_date_str = dialog_manager.dialog_data.get("selected_date")
    if not selected_date_str:
        return {"time_slots": [], "time_slots2": [], "count": 0, "count2": 0}
    
    try:
        selected_date = date.fromisoformat(selected_date_str)
    except ValueError:
        return {"time_slots": [], "time_slots2": [], "count": 0, "count2": 0}

    conn = await asyncpg.connect(DATABASE_URL)
    rows = await conn.fetch("SELECT time FROM book WHERE date = $1", selected_date.isoformat())
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

async def getter(dialog_manager: DialogManager, event_from_user, **kwargs):
    selected_date_str = dialog_manager.dialog_data.get("selected_date")
    if selected_date_str:
        try:
            selected_date = date.fromisoformat(selected_date_str)
        except ValueError:
            selected_date = None
    else:
        selected_date = None
        
    checked = dialog_manager.find("m_time_slots").get_checked()
    author = event_from_user.username or f"user_{event_from_user.id}"
    name = author

    if checked and selected_date:
        conn = await asyncpg.connect(DATABASE_URL)
        for t in checked:
            await conn.execute(
                "INSERT INTO book (name, date, time, author) VALUES ($1, $2, $3, $4)",
                name, selected_date.isoformat(), t, author
            )
        await conn.close()

    return {
        "date": selected_date.isoformat() if selected_date else "—",
        "author_user": author,
        "times": ", ".join(checked) if checked else "—",
    }

# ============= DIALOG =============
dialog = Dialog(
    Window(
        Format("Привет, {event.from_user.username}!"),
        CustomCalendar(id="cal", on_click=win1_on_date_selected),
        state=MySG.window1,
    ),
    Window(
        Const("Сначала выбери дату. Просто нажми на нужное число"),
        Const("Затем в нижней части выбери время. Можно несколько слотов"),
        Const("Когда дата нажата и галочки на нужное время стоят, то смело жми Забить!"),
        CustomCalendar(id="cal", on_click=win1_on_date_selected),
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
        Next(text=Const("Забить")),
        getter=get_time,
        state=MySG.window2,
    ),
    Window(
        Const("✅ Время забронировано"),
        Jinja(
            "<b>Дата</b>: {{date}}\n"
            "<b>Время</b>: {{times}}\n"
            "<b>Автор</b>: {{author_user}}\n"
        ),
        state=MySG.window3,
        getter=getter,
        parse_mode="html",
    ),
)

# ============= FASTAPI + AIogram =============
app = FastAPI()
storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=storage)

# Регистрируем диалог и настраиваем
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
    try:
        secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if secret != WEBHOOK_SECRET:
            return {"error": "Invalid secret"}
        update = await request.json()
        await dp.feed_raw_update(bot, update)
        return {"status": "ok"}
    except Exception as e:
        print(f"Webhook error: {e}")
        import traceback
        traceback.print_exc()
        return {"error": str(e)}

@app.get("/dashboard")
async def dashboard():
    conn = await asyncpg.connect(DATABASE_URL)
    rows = await conn.fetch("SELECT date, time, author FROM book ORDER BY date, time")
    await conn.close()

    html = """
    <html>
    <head><title>Бронирования</title></head>
    <body>
        <h2>Забронированные слоты</h2>
        <table border="1" cellpadding="8">
            <tr><th>Дата</th><th>Время</th><th>Автор</th></tr>
    """
    for r in rows:
        html += f"<tr><td>{r['date']}</td><td>{r['time']}</td><td>@{r['author']}</td></tr>"
    html += "</table></body></html>"
    return HTMLResponse(html)

@app.get("/")
async def root():
    return {"status": "OK", "dashboard": "/dashboard"}

# Правильный хендлер для /start
@dp.message(Command("start"))
async def start(message: Message, dialog_manager: DialogManager):
    await dialog_manager.start(MySG.window1, mode=StartMode.RESET_STACK)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
