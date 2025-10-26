# main.py
import os
import asyncio
from datetime import date
from datetime import datetime
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
    Button,  # заменили Next на Button
)
from aiogram_dialog.widgets.text import Const, Format, Jinja, Text
from aiogram_dialog.widgets.kbd.calendar_kbd import (
    DATE_TEXT,
    TODAY_TEXT,
    CalendarDaysView,
    CalendarMonthView,
    CalendarYearsView,
    CalendarScope,
    CalendarScopeView,
    CalendarConfig,  # убрано CalendarScopeView — не используется
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
    # 🔥 ДОБАВЛЕНО: защита от двойного бронирования
    await conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_booking 
        ON book (date, time)
    """)
    await conn.close()

# ============= CALENDAR WIDGETS =============
SELECTED_DAYS_KEY = "selected_dates"

class WeekDay(Text):
    async def _render_text(self, data, manager: DialogManager) -> str:
        selected_date: date = data["date"]
        locale = manager.event.from_user.language_code or "en"
        return get_day_names(width="short", context="stand-alone", locale=locale)[selected_date.weekday()].title()

class MarkedDay(Text):
    def __init__(self, mark: str, other):
        super().__init__()
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

# ============= STATES =============
class MySG(StatesGroup):
    window1 = State()
    window2 = State()
    window3 = State()

# ============= CALLBACKS =============
async def win1_on_date_selected(callback: CallbackQuery, widget, manager: DialogManager, selected_date: date):
    print(f"=== Date selected ===")
    print(f"Selected date: {selected_date}")
    print(f"User: {callback.from_user.username}")
    manager.dialog_data["selected_date"] = selected_date.isoformat()
    print(f"Saved to dialog_data: {selected_date.isoformat()}")
    try:
        await manager.next()
        print("Successfully moved to next state")
    except Exception as e:
        print(f"Error in manager.next(): {e}")
        import traceback
        traceback.print_exc()

async def get_time(dialog_manager: DialogManager, event_from_user, **kwargs):
    print("get_time called")
    selected_date_str = dialog_manager.dialog_data.get("selected_date")
    print(f"Selected date string: {selected_date_str}")
    
    if not selected_date_str:
        return {"time_slots": [], "time_slots2": [], "count": 0, "count2": 0}
    
    try:
        selected_date = date.fromisoformat(selected_date_str)
        print(f"Parsed date: {selected_date}")
    except ValueError as e:
        print(f"Date parsing error: {e}")
        return {"time_slots": [], "time_slots2": [], "count": 0, "count2": 0}

    conn = await asyncpg.connect(DATABASE_URL)
    rows = await conn.fetch("SELECT time FROM book WHERE date = $1", selected_date)  # ✅ объект date
    booked_times = {row["time"] for row in rows}
    await conn.close()

    time_slots_zero1 = ["8:00", "9:00", "10:00", "11:00", "12:00", "13:00", "14:00", "15:00"]
    time_slots_zero2 = ["16:00", "17:00", "18:00", "19:00", "20:00", "21:00", "22:00", "23:00"]

    time_slots = [(t, t) for t in time_slots_zero1 if t not in booked_times]
    time_slots2 = [(t, t) for t in time_slots_zero2 if t not in booked_times]

    result = {
        "time_slots": time_slots,
        "time_slots2": time_slots2,
        "count": len(time_slots),
        "count2": len(time_slots2),
    }
    print(f"Time slots result: {result}")
    return result

# 🔥 НОВАЯ ФУНКЦИЯ: обработка нажатия "Забить"
async def on_book_click(callback: CallbackQuery, button, manager: DialogManager):
    from asyncpg import UniqueViolationError

    selected_date_str = manager.dialog_data.get("selected_date")
    if not selected_date_str:
        await callback.answer("❌ Не выбрана дата!", show_alert=True)
        return

    try:
        selected_date = date.fromisoformat(selected_date_str)
    except ValueError:
        await callback.answer("❌ Ошибка даты!", show_alert=True)
        return

    # 🔥 БЕРЁМ ВЫБОР ИЗ ОБОИХ СПИСКОВ
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
        for t in checked:
            # ✅ Передаём объект date, а не строку
            await conn.execute(
                "INSERT INTO book (name, date, time, author) VALUES ($1, $2, $3, $4)",
                name, selected_date, t, author
            )
        # Сохраняем для финального экрана
        manager.dialog_data.update({
            "final_date": selected_date.isoformat(),
            "final_times": checked,
            "final_author": author
        })
        await manager.next()
    except UniqueViolationError:
        await callback.answer("⚠️ Слот уже занят! Выбери другое время.", show_alert=True)
    finally:
        await conn.close()

# 🔥 НОВАЯ ФУНКЦИЯ: только отображение результата (без записи в БД!)
async def final_getter(dialog_manager: DialogManager, **kwargs):
    data = dialog_manager.dialog_data
    return {
        "date": data.get("final_date", "—"),
        "author_user": data.get("final_author", "—"),
        "times": ", ".join(data.get("final_times", [])) or "—",
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
        # 🔥 ЗАМЕНА: Next → Button с обработчиком
        Button(Const("Забить"), id="book_btn", on_click=on_book_click),
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
        getter=final_getter,  # 🔥 новая функция без записи в БД
        parse_mode="html",
    ),
)

# ============= FASTAPI + AIogram =============
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

    # Подсчет статистики
    total_bookings = len(rows)
    today = date.today()
    today_bookings = len([r for r in rows if r['date'] == today])
    
    # Группировка по датам
    from collections import defaultdict
    bookings_by_date = defaultdict(list)
    for r in rows:
        bookings_by_date[r['date']].append(r)

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>📅 Бронирования</title>
        <meta charset="utf-8">
        <style>
            body {{
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                margin: 0;
                padding: 20px;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
            }}
            .container {{
                max-width: 1200px;
                margin: 0 auto;
                background: white;
                border-radius: 15px;
                box-shadow: 0 10px 30px rgba(0,0,0,0.3);
                overflow: hidden;
            }}
            .header {{
                background: linear-gradient(135deg, #4facfe 0%, #00f2fe 100%);
                color: white;
                padding: 30px;
                text-align: center;
            }}
            .stats {{
                display: flex;
                justify-content: space-around;
                background: #f8f9fa;
                padding: 20px;
                border-bottom: 1px solid #e9ecef;
            }}
            .stat-box {{
                text-align: center;
                padding: 15px;
            }}
            .stat-number {{
                font-size: 2em;
                font-weight: bold;
                color: #4facfe;
            }}
            .stat-label {{
                color: #6c757d;
                font-size: 0.9em;
            }}
            .content {{
                padding: 30px;
            }}
            h2 {{
                color: #333;
                text-align: center;
                margin-bottom: 30px;
            }}
            table {{
                width: 100%;
                border-collapse: collapse;
                margin-top: 20px;
                box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            }}
            th {{
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
                padding: 15px;
                text-align: left;
                font-weight: 600;
            }}
            td {{
                padding: 12px 15px;
                border-bottom: 1px solid #e9ecef;
            }}
            tr:hover {{
                background-color: #f8f9fa;
                transform: scale(1.01);
                transition: all 0.2s ease;
            }}
            tr:nth-child(even) {{
                background-color: #f8f9fa;
            }}
            .date-header {{
                background: #e9ecef;
                font-weight: bold;
                font-size: 1.1em;
                border-left: 4px solid #4facfe;
            }}
            .no-bookings {{
                text-align: center;
                color: #6c757d;
                font-style: italic;
                padding: 40px;
            }}
            .footer {{
                text-align: center;
                padding: 20px;
                color: #6c757d;
                font-size: 0.9em;
                border-top: 1px solid #e9ecef;
            }}
            @media (max-width: 768px) {{
                .stats {{
                    flex-direction: column;
                }}
                .container {{
                    margin: 10px;
                }}
                table {{
                    font-size: 0.9em;
                }}
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>📅 Панель бронирований</h1>
                <p>Управление временными слотами</p>
            </div>
            
            <div class="stats">
                <div class="stat-box">
                    <div class="stat-number">{total_bookings}</div>
                    <div class="stat-label">Всего бронирований</div>
                </div>
                <div class="stat-box">
                    <div class="stat-number">{today_bookings}</div>
                    <div class="stat-label">Сегодня</div>
                </div>
                <div class="stat-box">
                    <div class="stat-number">{len(bookings_by_date)}</div>
                    <div class="stat-label">Дней с бронями</div>
                </div>
            </div>
            
            <div class="content">
                <h2>📋 Забронированные слоты</h2>
    """
    
    if not rows:
        html += '<div class="no-bookings">Пока нет бронирований</div>'
    else:
        current_date = None
        for r in rows:
            if r['date'] != current_date:
                if current_date is not None:
                    html += '</tbody></table><br>'
                current_date = r['date']
                html += f'''
                <table>
                    <thead>
                        <tr class="date-header">
                            <th colspan="3">📅 {current_date}</th>
                        </tr>
                        <tr>
                            <th>Время</th>
                            <th>Автор</th>
                            <th>Действия</th>
                        </tr>
                    </thead>
                    <tbody>
                '''
            
            # Определяем цвет для времени
            time_hour = int(r['time'].split(':')[0])
            time_color = "#28a745" if time_hour < 16 else "#ffc107"
            
            html += f'''
                <tr>
                    <td>
                        <span style="color: {time_color}; font-weight: bold;">⏰ {r['time']}</span>
                    </td>
                    <td>
                        <span style="color: #007bff;">👤 @{r['author']}</span>
                    </td>
                    <td>
                        <button onclick="alert('Функция удаления пока недоступна')" 
                                style="background: #dc3545; color: white; border: none; padding: 5px 10px; border-radius: 3px; cursor: pointer;">
                            ❌ Удалить
                        </button>
                    </td>
                </tr>
            '''
        
        html += '</tbody></table>'
    
    html += """
            </div>
            <div class="footer">
                <p>📊 Система бронирования | Обновлено: {datetime}</p>
            </div>
        </div>
    </body>
    </html>
    """.format(datetime=datetime.now().strftime("%d.%m.%Y %H:%M"))
    
    return HTMLResponse(html)


@app.get("/")
async def root():
    return {"status": "OK", "dashboard": "/dashboard"}

@dp.message(Command("start"))
async def start(message: Message, dialog_manager: DialogManager):
    print(f"/start command received from {message.from_user.username}")
    await dialog_manager.start(MySG.window1, mode=StartMode.RESET_STACK)
    print("Dialog started")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=WEB_SERVER_PORT)