import json
import logging
import requests
import asyncio
import os
import httpx
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    ConversationHandler,
    filters,
)
from asyncio import Task, create_task, sleep
from dotenv import load_dotenv

# Настройки логирования
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Загрузка переменных окружения из файла .env
load_dotenv()
WB_API_KEY = "eyJhbGciOiJFUzI1NiIsImtpZCI6IjIwMjQxMTE4djEiLCJ0eXAiOiJKV1QifQ.eyJlbnQiOjEsImV4cCI6MTc0OTQzNDI0NSwiaWQiOiIwMTkzYTY4Zi04YTNhLTc0NzQtODgzNy0xMThlNTEyNzI0ZjEiLCJpaWQiOjU4NjU2MzkzLCJvaWQiOjQyNTU1NzcsInMiOjEwNDAsInNpZCI6IjFmNTRhOGY5LWRlNTEtNDg5ZC04MWZiLWZlOTAwNDlkZmRmMSIsInQiOmZhbHNlLCJ1aWQiOjU4NjU2MzkzfQ.i-ii2dLXASqt68Q5nn8nKGmRccfB30pD3mn6fOfDvZMSpjqaTA6QBKYkk2M0KIjvD9RpmnZTsuIsrxpkKZwMpA"
TELEGRAM_BOT_TOKEN = "8028999606:AAFYdlfmELLfQ388Y19ktkTrmUEfw-9XQAI"

# Глобальные переменные
monitored_warehouses = set()
selected_types = set()
monitoring_min_days = 0  # Минимальный интервал мониторинга (в днях)
monitoring_max_days = 7  # Максимальный интервал мониторинга (в днях)
max_coefficient = 10  # Максимальный коэффициент по умолчанию
is_monitoring = False
supply_data_global = {}  # Данные о поставке
monitoring_task: Task = None  # Глобальная переменная для хранения задачи
previous_data = {}  # Хранит предыдущие результаты мониторинга
acceptance_type_emojis = {
    "Короба": "📦",
    "QR-поставка": "🏷️",
    "Монопаллеты": "🛠️",
    "Суперсейф": "🔒",
}

# Типы приемки
acceptance_types = ["Короба", "QR-поставка", "Монопаллеты", "Суперсейф"]

# Определяем этапы разговора для ввода поставки
SUPPLY_NAME, SUPPLY_PRODUCTS, SUPPLY_BOXES = range(3)

# Загрузка складов из файла
def load_warehouses():
    try:
        with open("warehouses.json", "r", encoding="utf-8") as file:
            warehouses = json.load(file)
            if not warehouses:
                logger.warning("Файл складов пуст. Проверьте содержимое.")
            return warehouses
    except FileNotFoundError:
        logger.error("Файл warehouses.json не найден.")
    except json.JSONDecodeError as e:
        logger.error(f"Ошибка чтения файла складов: {e}")
    return []

all_warehouses = load_warehouses()

def is_similar(warehouse_api_name, warehouse_monitor_name, threshold=0.7):
    similarity = SequenceMatcher(
        None, warehouse_api_name.lower(), warehouse_monitor_name.lower()
    ).ratio()
    return similarity >= threshold

def generate_warehouse_id(warehouse: str) -> str:
    return str(abs(hash(warehouse)) % (10 ** 8))

# Стартовая команда
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Стартовое меню бота с ReplyKeyboardMarkup."""
    logger.info("Пользователь начал взаимодействие с ботом.")
    keyboard = [
        ["Настройки мониторинга"],
        ["Запустить мониторинг", "Остановить мониторинг"],
        ["Проверить поставки"],  # Новая кнопка
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(
        "Добро пожаловать! Используйте меню ниже для взаимодействия с ботом:",
        reply_markup=reply_markup,
    )


# Получение данных из Wildberries
async def fetch_acceptance_coefficients():
    logger.info("Получение данных о коэффициентах из Wildberries API.")
    headers = {"Authorization": WB_API_KEY, "Content-Type": "application/json"}
    url = "https://supplies-api.wildberries.ru/api/v1/acceptance/coefficients"

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()
        logger.info(f"Данные успешно получены: {len(data)} записей.")
        if data:
            logger.info(f"Первый элемент данных: {data[0]}")
        return data
    except httpx.RequestError as e:
        logger.error(f"Ошибка при подключении к Wildberries API: {e}")
    except httpx.HTTPStatusError as e:
        logger.error(f"Ошибка ответа API: {e.response.status_code}")
    return []


# Настройки мониторинга
async def monitoring_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Пользователь открыл настройки мониторинга.")
    keyboard = [
        [
            InlineKeyboardButton(
                "Выбрать диапазон дат", callback_data="set_interval"
            )
        ],
        [
            InlineKeyboardButton(
                "Изменить коэффициент", callback_data="set_coefficient"
            )
        ],
        [
            InlineKeyboardButton(
                "Выбрать склады", callback_data="set_warehouses"
            )
        ],
        [
            InlineKeyboardButton(
                "Выбрать тип приемки", callback_data="set_acceptance_types"
            )
        ],
        [
            InlineKeyboardButton(
                "Назад", callback_data="go_back"
            )
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    monitoring_message = (
        f"Текущие настройки:\n"
        f"Диапазон дат: от {monitoring_min_days} до {monitoring_max_days} дней\n"
        f"Максимальный коэффициент: {max_coefficient}\n"
        f"Склады: {', '.join(monitored_warehouses) if monitored_warehouses else 'Все'}\n"
        f"Типы приемки: {', '.join(selected_types) if selected_types else 'Все'}"
    )
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            monitoring_message, reply_markup=reply_markup
        )
    else:
        await update.message.reply_text(monitoring_message, reply_markup=reply_markup)

# Функция для обработки кнопки "Назад"
async def go_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    logger.info("Пользователь вернулся в главное меню.")
    keyboard = [
        ["Настройки мониторинга"],
        ["Запустить мониторинг", "Остановить мониторинг"],
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await query.message.reply_text(
        "Вы вернулись в главное меню.", reply_markup=reply_markup
    )

# Установка диапазона мониторинга
async def set_interval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Введите минимальное количество дней от сегодняшней даты:")
    context.user_data["awaiting_min_days"] = True

async def handle_min_days_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.get("awaiting_min_days"):
        try:
            min_days = int(update.message.text)
            if min_days < 0:
                await update.message.reply_text("Минимальное количество дней не может быть отрицательным. Попробуйте снова.")
                return
            context.user_data["min_days"] = min_days
            context.user_data["awaiting_min_days"] = False
            await update.message.reply_text("Введите максимальное количество дней от сегодняшней даты:")
            context.user_data["awaiting_max_days"] = True
        except ValueError:
            await update.message.reply_text("Ошибка: введите целое число.")

async def handle_max_days_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.get("awaiting_max_days"):
        try:
            max_days = int(update.message.text)
            min_days = context.user_data.get("min_days")
            if max_days < min_days:
                await update.message.reply_text("Ошибка: максимальное количество дней не может быть меньше минимального. Попробуйте снова.")
                return
            context.user_data["max_days"] = max_days
            context.user_data["awaiting_max_days"] = False
            global monitoring_min_days, monitoring_max_days
            monitoring_min_days = min_days
            monitoring_max_days = max_days
            logger.info(f"Установлен диапазон мониторинга: от {monitoring_min_days} до {monitoring_max_days} дней.")
            await update.message.reply_text(f"Диапазон мониторинга установлен: от {monitoring_min_days} до {monitoring_max_days} дней.")
            await monitoring_settings(update, context)
        except ValueError:
            await update.message.reply_text("Ошибка: введите целое число.")

# Функции для выбора типов приемки
async def select_acceptance_types(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Пользователь начал выбор типов приемки.")
    query = update.callback_query
    await query.answer()

    keyboard = [
        [
            InlineKeyboardButton(
                f"{acceptance} {'✅' if acceptance in selected_types else '❌'}",
                callback_data=f"toggle_type_{acceptance}",
            )
        ]
        for acceptance in acceptance_types
    ]
    keyboard.append(
        [InlineKeyboardButton("Завершить выбор", callback_data="finish_acceptance_types")]
    )
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "Выберите типы приемки для мониторинга:", reply_markup=reply_markup
    )

async def toggle_acceptance_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    type_name = query.data.split("_", 2)[2]
    if type_name in selected_types:
        selected_types.remove(type_name)
        logger.info(f"Тип приемки '{type_name}' удален из списка мониторинга.")
    else:
        selected_types.add(type_name)
        logger.info(f"Тип приемки '{type_name}' добавлен в список мониторинга.")

    await select_acceptance_types(update, context)

async def finish_acceptance_types(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Пользователь завершил выбор типов приемки.")
    query = update.callback_query
    await query.answer()
    await monitoring_settings(update, context)

# Работа со складами
warehouses = load_warehouses()
per_page = 5  # Количество складов на одной странице

def paginate_warehouses(page):
    start = (page - 1) * per_page
    end = start + per_page
    return warehouses[start:end]

# Обработчик для кнопки "Выбрать склады"
async def select_warehouses(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Пользователь начал выбор складов.")
    context.user_data["current_page"] = 1
    await show_warehouses(update, context, context.user_data["current_page"])

async def show_warehouses(
    update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = None
):
    logger.info(f"Отображаем страницу {page} складов.")

    if not page:
        callback_data = update.callback_query.data
        if callback_data.startswith("page_"):
            page = int(callback_data.split("_")[1])
        else:
            page = 1

    total_pages = (len(all_warehouses) + per_page - 1) // per_page
    current_warehouses = paginate_warehouses(page)

    keyboard = [
        [
            InlineKeyboardButton(
                f"{warehouse} {'✅' if warehouse in monitored_warehouses else '❌'}",
                callback_data=f"toggle_{generate_warehouse_id(warehouse)}",
            )
        ]
        for warehouse in current_warehouses
    ]

    navigation_buttons = []
    if page > 1:
        navigation_buttons.append(
            InlineKeyboardButton("<", callback_data=f"page_{page - 1}")
        )
    if page < total_pages:
        navigation_buttons.append(
            InlineKeyboardButton(">", callback_data=f"page_{page + 1}")
        )
    if page > 3:
        navigation_buttons.insert(
            0, InlineKeyboardButton("<<<", callback_data="page_1")
        )
    if page < total_pages - 2:
        navigation_buttons.append(
            InlineKeyboardButton(">>>", callback_data=f"page_{total_pages}")
        )

    if navigation_buttons:
        keyboard.append(navigation_buttons)

    keyboard.append(
        [InlineKeyboardButton("Завершить выбор", callback_data="finish_warehouses")]
    )

    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.callback_query.edit_message_text(
        f"Выберите склады (страница {page}/{total_pages}):", reply_markup=reply_markup
    )

async def page_navigation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    page = int(query.data.split("_")[1])
    context.user_data["current_page"] = page
    await show_warehouses(update, context, page)

async def toggle_warehouse(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Переключение состояния склада.")
    query = update.callback_query
    await query.answer()

    warehouse_id = query.data.split("_")[1]
    warehouse_name = next(
        (w for w in all_warehouses if generate_warehouse_id(w) == warehouse_id), None
    )

    if not warehouse_name:
        logger.error(f"Склад с ID {warehouse_id} не найден.")
        return

    if warehouse_name in monitored_warehouses:
        monitored_warehouses.remove(warehouse_name)
        logger.info(f"Склад {warehouse_name} удален из списка мониторинга.")
    else:
        monitored_warehouses.add(warehouse_name)
        logger.info(f"Склад {warehouse_name} добавлен в список мониторинга.")

    current_page = context.user_data.get("current_page", 1)
    await show_warehouses(update, context, current_page)

async def finish_warehouses(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Пользователь завершил выбор складов.")
    query = update.callback_query
    await query.answer()

    await monitoring_settings(update, context)

# Изменение коэффициента
async def set_monitoring_coefficient(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    logger.info("Пользователь выбрал изменить коэффициент.")
    await query.edit_message_text("Введите максимальный коэффициент (0 = бесплатно):")
    context.user_data["awaiting_coefficient"] = True

async def handle_coefficient_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if context.user_data.get("awaiting_coefficient"):
        try:
            global max_coefficient
            max_coefficient = int(update.message.text)
            context.user_data["awaiting_coefficient"] = False
            logger.info(f"Установлен максимальный коэффициент: {max_coefficient}.")
            await update.message.reply_text(
                f"Максимальный коэффициент установлен: {max_coefficient}"
            )
            await monitoring_settings(update, context)
        except ValueError:
            await update.message.reply_text("Ошибка: введите целое число.")

# Команда для ввода данных о поставке
async def set_supply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Начинает диалог для ввода данных поставки."""
    await update.message.reply_text("Пожалуйста, введите название поставки:")
    return SUPPLY_NAME

async def supply_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Сохраняет название поставки и запрашивает информацию о товарах."""
    context.user_data['supply_data'] = {'name': update.message.text}
    await update.message.reply_text(
        "Введите информацию о товарах в формате 'barcode:quantity', по одному на строку.\nКогда закончите, отправьте 'Готово'."
    )
    context.user_data['products'] = []
    return SUPPLY_PRODUCTS

async def show_supplies(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отображает список поставок с кнопками для выбора."""
    logger.info("Запрашиваем список поставок через API.")
    supplies = get_supply_list()

    # Проверяем корректность ответа
    if not supplies or not isinstance(supplies, dict) or "supplies" not in supplies:
        await update.message.reply_text("Ошибка: Не удалось получить список поставок.")
        logger.error(f"Ответ API для списка поставок: {supplies}")
        return

    supplies_list = supplies.get("supplies", [])
    if not supplies_list:
        await update.message.reply_text("Нет доступных поставок.")
        return

    # Создаём кнопки для выбора поставок
    keyboard = [
        [InlineKeyboardButton(f"Поставка ID: {supply['id']}", callback_data=f"supply_{supply['id']}")]
        for supply in supplies_list
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Выберите поставку для подробной информации:", reply_markup=reply_markup)



async def show_supply_details(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отображает детали выбранной поставки."""
    query = update.callback_query
    await query.answer()

    supply_id = query.data.split("_")[1]
    logger.info(f"Получение информации о поставке с ID: {supply_id}")
    supply_details = get_supply_details(supply_id)

    if not supply_details:
        await query.edit_message_text("Ошибка: Не удалось получить информацию о поставке.")
        return

    # Форматируем информацию о поставке
    details = (
        f"Поставка ID: {supply_details['id']}\n"
        f"Название: {supply_details['name']}\n"
        f"Создана: {supply_details['createdAt']}\n"
        f"Состояние: {'Завершена' if supply_details.get('done') else 'Ожидает'}"
    )
    await query.edit_message_text(details)




async def supply_products(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Сохраняет информацию о товарах."""
    text = update.message.text
    if text.lower() == 'готово':
        if not context.user_data['products']:
            await update.message.reply_text("Вы не ввели ни одного товара. Пожалуйста, введите хотя бы один товар.")
            return SUPPLY_PRODUCTS
        else:
            context.user_data['supply_data']['products'] = context.user_data['products']
            await update.message.reply_text(
                "Теперь введите информацию о коробках в формате 'id:barcode', по одной на строку.\nКогда закончите, отправьте 'Готово'."
            )
            context.user_data['boxes'] = []
            return SUPPLY_BOXES
    else:
        # Обрабатываем введенную информацию о товаре
        try:
            barcode, quantity = text.split(':')
            product = {'barcode': barcode.strip(), 'quantity': int(quantity.strip())}
            context.user_data['products'].append(product)
            await update.message.reply_text(
                "Товар добавлен. Введите следующий или отправьте 'Готово'."
            )
        except ValueError:
            await update.message.reply_text("Ошибка ввода. Пожалуйста, используйте формат 'barcode:quantity'.")
        return SUPPLY_PRODUCTS

async def supply_boxes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Сохраняет информацию о коробках."""
    text = update.message.text
    if text.lower() == 'готово':
        if not context.user_data['boxes']:
            await update.message.reply_text("Вы не ввели ни одной коробки. Пожалуйста, введите хотя бы одну коробку.")
            return SUPPLY_BOXES
        else:
            # Завершаем ввод данных
            supply_data = context.user_data['supply_data']
            supply_data['boxes'] = context.user_data['boxes']
            global supply_data_global
            supply_data_global = supply_data
            await update.message.reply_text("Данные поставки успешно сохранены!")
            if context.user_data.get("autobooking_enabled"):
                global is_monitoring, monitoring_task
                is_monitoring = True
                monitoring_task = create_task(monitoring_loop(update, context))
                await update.message.reply_text("Мониторинг запущен с автобронированием!")
            return ConversationHandler.END
    else:
        # Обрабатываем введенную информацию о коробке
        try:
            box_id, barcode = text.split(':')
            box = {'id': box_id.strip(), 'barcode': barcode.strip()}
            context.user_data['boxes'].append(box)
            await update.message.reply_text(
                "Коробка добавлена. Введите следующую или отправьте 'Готово'."
            )
        except ValueError:
            await update.message.reply_text("Ошибка ввода. Пожалуйста, используйте формат 'id:barcode'.")
        return SUPPLY_BOXES

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отменяет ввод данных поставки."""
    await update.message.reply_text("Ввод данных поставки отменен.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

# Создание поставки и привязка к приемке


def format_coefficients(data):
    logger.info("Форматирование данных для отправки.")
    formatted = []
    for item in data:
        warehouse = item.get("warehouseName", "Неизвестный склад")
        delivery_datetime_str = item.get("date")
        delivery_datetime = datetime.fromisoformat(delivery_datetime_str.replace('Z', '+00:00'))
        delivery_datetime = delivery_datetime.replace(tzinfo=None)        
        date_str = delivery_datetime.strftime("%d-%m %H:%M")
        coefficient = item.get("coefficient", -1)

        box_type = item.get("boxTypeName", "Неизвестный тип")
        box_emoji = acceptance_type_emojis.get(box_type, "❓")

        if coefficient == 0:
            status = "🟩 FREE"
        elif coefficient <= 10:
            status = f"🟦 {coefficient}"
        else:
            status = f"🟧 {coefficient}"

        formatted.append(f"{box_emoji} [{status}] [{warehouse}] [{date_str}] ")
    logger.info(f"Форматирование завершено. Подготовлено {len(formatted)} записей.")
    return formatted



def filter_coefficients(data, min_days, max_days, max_coeff):
    logger.info("Фильтрация данных по складам, диапазону дат, коэффициентам и типу приемки.")
    filtered = []
    start_date = datetime.now() + timedelta(days=min_days)
    end_date = datetime.now() + timedelta(days=max_days)

    for item in data:
        warehouse = item.get("warehouseName", "Неизвестный склад")
        warehouse_id = item.get("warehouseID")
        delivery_datetime_str = item.get("date")
        if not delivery_datetime_str:
            continue
        delivery_datetime = datetime.fromisoformat(delivery_datetime_str.replace('Z', '+00:00'))
        delivery_datetime = delivery_datetime.replace(tzinfo=None)        
        coefficient = item.get("coefficient", None)
        box_type = item.get("boxTypeName", "")

        if coefficient is None or coefficient < 0:
            continue

        if (not monitored_warehouses or warehouse in monitored_warehouses) and (
            not selected_types or box_type in selected_types
        ):
            if start_date <= delivery_datetime <= end_date and coefficient <= max_coeff:
                filtered.append(item)

    logger.info(f"Отфильтровано {len(filtered)} записей из {len(data)}.")
    return filtered



# Запуск мониторинга
async def start_monitoring(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        chat_id = query.message.chat_id
    else:
        query = None
        chat_id = update.effective_chat.id  # Получаем chat_id из сообщения

    keyboard = [
        [
            InlineKeyboardButton("Да", callback_data="enable_autobooking"),
            InlineKeyboardButton("Нет", callback_data="disable_autobooking"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = "Включить автобронирование?"

    if query:
        await query.edit_message_text(text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(text, reply_markup=reply_markup)

async def enable_autobooking(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["autobooking_enabled"] = True
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        await query.edit_message_text("Вы выбрали автобронирование. Пожалуйста, введите данные поставки с помощью команды /set_supply.")
    else:
        await update.message.reply_text("Вы выбрали автобронирование. Пожалуйста, введите данные поставки с помощью команды /set_supply.")

async def disable_autobooking(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["autobooking_enabled"] = False
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        await query.edit_message_text("Мониторинг запущен без автобронирования!")
    else:
        await update.message.reply_text("Мониторинг запущен без автобронирования!")
    global is_monitoring, monitoring_task
    is_monitoring = True
    monitoring_task = create_task(monitoring_loop(update, context))

# Функция для получения списка поставок
def get_supply_list(limit=100, next_value=0):
    """
    Получает список поставок с использованием параметров пагинации.
    :param limit: Количество записей на одну страницу (1..1000).
    :param next_value: Значение для начала пагинации, 0 для первого запроса.
    :return: JSON-ответ с данными поставок или None в случае ошибки.
    """
    url = "https://marketplace-api.wildberries.ru/api/v3/supplies"
    headers = {"Authorization": WB_API_KEY}
    params = {"limit": limit, "next": next_value}

    try:
        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()
        data = response.json()
        logger.info(f"Получен ответ API: {data}")
        return data
    except requests.exceptions.RequestException as e:
        logger.error(f"Ошибка при получении списка поставок: {e}")
        return None





# Функция для получения информации о конкретной поставке
def get_supply_details(supply_id):
    """
    Получает информацию о конкретной поставке по её ID.
    :param supply_id: ID поставки.
    :return: JSON-ответ с информацией о поставке или None в случае ошибки.
    """
    url = f"https://marketplace-api.wildberries.ru/api/v3/supplies/{supply_id}"
    headers = {"Authorization": WB_API_KEY}

    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"Ошибка при получении информации о поставке {supply_id}: {e}")
        return None



# Обновленная функция создания поставки
def create_supply(supply_data, acceptance_data):
    """Создает поставку и привязывает ее к приемке через API Wildberries."""
    url = "https://marketplace-api.wildberries.ru/api/v3/supplies"
    headers = {"Authorization": WB_API_KEY, "Content-Type": "application/json"}
    try:
        delivery_datetime = acceptance_data.get("date")
        payload = {
            "name": supply_data.get("name"),
            "type": "fbs",
            "deliveries": [
                {
                    "warehouseId": acceptance_data.get("warehouseID"),
                    "deliveryPlannedAt": delivery_datetime,
                    "products": supply_data.get("products"),
                    "boxes": supply_data.get("boxes"),
                }
            ],
        }
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status()
        supply_info = response.json()
        supply_id = supply_info.get("id")
        logger.info(f"Поставка успешно создана: {supply_id}")

        # Получаем информацию о только что созданной поставке
        supply_details = get_supply_details(supply_id)
        if supply_details:
            logger.info(f"Детали созданной поставки: {supply_details}")

        # Получаем список всех поставок
        supply_list = get_supply_list()
        if supply_list:
            logger.info(f"Список всех поставок: {supply_list}")

        return supply_id, supply_details, supply_list
    except requests.exceptions.RequestException as e:
        logger.error(f"Ошибка создания и привязки поставки: {e}")
        return None, None, None


# Обновленный блок обработки результатов автобронирования
async def handle_autobooking_results(update, context, supply_id, supply_details, supply_list):
    chat_id = update.effective_chat.id

    # Отправляем информацию о только что созданной поставке
    if supply_details:
        supply_info_message = (
            f"Поставка успешно создана!\n"
            f"ID: {supply_id}\n"
            f"Склад: {supply_details.get('warehouseId', 'Неизвестно')}\n"
            f"Дата: {supply_details.get('deliveryPlannedAt', 'Не указано')}\n"
            f"Товары: {supply_details.get('products', 'Нет данных')}\n"
            f"Коробки: {supply_details.get('boxes', 'Нет данных')}\n"
        )
        await context.bot.send_message(chat_id, supply_info_message)

    # Отправляем список всех поставок
    if supply_list:
        supply_list_message = "Список всех поставок:\n"
        for supply in supply_list:
            supply_list_message += (
                f"ID: {supply.get('id', 'Неизвестно')}, "
                f"Склад: {supply.get('warehouseId', 'Неизвестно')}, "
                f"Дата: {supply.get('deliveryPlannedAt', 'Не указано')}\n"
            )
        await context.bot.send_message(chat_id, supply_list_message)



async def monitoring_loop(update, context) -> None:
    """Основной цикл мониторинга с поддержкой автобронирования."""
    global is_monitoring, previous_data, supply_data_global
    autobooking_enabled = context.user_data.get("autobooking_enabled", False)
    chat_id = update.effective_chat.id

    try:
        while is_monitoring:
            logger.info("Запрос данных для мониторинга.")
            coefficients = await fetch_acceptance_coefficients()

            if coefficients:
                # Фильтрация данных
                filtered = filter_coefficients(
                    coefficients, monitoring_min_days, monitoring_max_days, max_coefficient
                )
                # Сортировка по коэффициенту
                filtered.sort(key=lambda x: x.get("coefficient", float('inf')))
                new_data = {
                    f"{item['warehouseName']}-{item['date']}": item
                    for item in filtered
                }
                new_items = [
                    item for key, item in new_data.items() if key not in previous_data
                ]

                if new_items:
                    logger.info(f"Обнаружено {len(new_items)} новых записей.")
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text="\n".join(format_coefficients(new_items))
                    )
                    previous_data.update(new_data)

                    if autobooking_enabled:
                        if not supply_data_global:
                            await context.bot.send_message(
                                chat_id=chat_id,
                                text="Перед запуском мониторинга с автобронированием укажите данные поставки с помощью команды /set_supply."
                            )
                            is_monitoring = False
                            break

                        # Выбираем приемку с минимальным коэффициентом
                        best_acceptance = new_items[0]
                        supply_id = create_supply(supply_data_global, best_acceptance)

                        if supply_id:
                            is_monitoring = False
                            await context.bot.send_message(
                                chat_id=chat_id,
                                text=f"Поставка успешно создана и привязана к приемке с коэффициентом {best_acceptance['coefficient']}."
                            )
                            break
                        else:
                            await context.bot.send_message(
                                chat_id=chat_id,
                                text="Ошибка при создании поставки. Продолжаем мониторинг."
                            )
                            continue

            await asyncio.sleep(10)  # Период запроса к API
    except asyncio.CancelledError:
        logger.info("Мониторинг был остановлен.")
    except Exception as e:
        logger.error(f"Ошибка в цикле мониторинга: {e}")
    finally:
        is_monitoring = False



async def stop_monitoring(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global is_monitoring, monitoring_task
    if update.callback_query:
        query = update.callback_query
        await query.answer()
    else:
        query = None

    if not monitoring_task or monitoring_task.done():
        if query:
            await query.edit_message_text("Мониторинг не был запущен.")
        else:
            await update.message.reply_text("Мониторинг не был запущен.")
        return

    is_monitoring = False
    monitoring_task.cancel()
    try:
        await monitoring_task
    except asyncio.CancelledError:
        logger.info("Задача мониторинга успешно отменена.")

    if query:
        await query.edit_message_text("Мониторинг остановлен.")
    else:
        await update.message.reply_text("Мониторинг остановлен.")

# Обработчики команд и сообщений
async def handle_monitoring_settings(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    logger.info("Кнопка 'Настройки мониторинга' нажата.")
    await monitoring_settings(update, context)

async def handle_start_monitoring(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    logger.info("Кнопка 'Запустить мониторинг' нажата.")
    await start_monitoring(update, context)

async def handle_stop_monitoring(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    logger.info("Кнопка 'Остановить мониторинг' нажата.")
    await stop_monitoring(update, context)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Получено сообщение: {update.message.text}")
    if context.user_data.get("awaiting_coefficient"):
        await handle_coefficient_input(update, context)
    elif context.user_data.get("awaiting_min_days"):
        await handle_min_days_input(update, context)
    elif context.user_data.get("awaiting_max_days"):
        await handle_max_days_input(update, context)
    else:
        await update.message.reply_text("Пожалуйста, выберите действие из меню.")

# Запуск приложения
def main():
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Определяем ConversationHandler для ввода данных поставки
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('set_supply', set_supply)],
        states={
            SUPPLY_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, supply_name)],
            SUPPLY_PRODUCTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, supply_products)],
            SUPPLY_BOXES: [MessageHandler(filters.TEXT & ~filters.COMMAND, supply_boxes)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )
    application.add_handler(conv_handler)

    # Добавляем CommandHandler для команд
    application.add_handler(CommandHandler("start", start))

    # Добавляем CallbackQueryHandler для обработки callback_data из InlineKeyboardButton
    application.add_handler(CallbackQueryHandler(set_interval, pattern="^set_interval$"))
    application.add_handler(CallbackQueryHandler(set_monitoring_coefficient, pattern="^set_coefficient$"))
    application.add_handler(CallbackQueryHandler(select_acceptance_types, pattern="^set_acceptance_types$"))
    application.add_handler(CallbackQueryHandler(toggle_acceptance_type, pattern="^toggle_type_"))
    application.add_handler(CallbackQueryHandler(finish_acceptance_types, pattern="^finish_acceptance_types$"))
    application.add_handler(CallbackQueryHandler(toggle_warehouse, pattern="^toggle_"))
    application.add_handler(CallbackQueryHandler(select_warehouses, pattern="^set_warehouses$"))
    application.add_handler(CallbackQueryHandler(page_navigation, pattern="^page_"))
    application.add_handler(CallbackQueryHandler(finish_warehouses, pattern="^finish_warehouses$"))
    application.add_handler(CallbackQueryHandler(monitoring_settings, pattern="^monitoring_settings$"))
    application.add_handler(CallbackQueryHandler(enable_autobooking, pattern="^enable_autobooking$"))
    application.add_handler(CallbackQueryHandler(disable_autobooking, pattern="^disable_autobooking$"))
    application.add_handler(CallbackQueryHandler(start_monitoring, pattern="^start_monitoring$"))
    application.add_handler(CallbackQueryHandler(stop_monitoring, pattern="^stop_monitoring$"))
    application.add_handler(CallbackQueryHandler(go_back, pattern="^go_back$"))
    application.add_handler(CallbackQueryHandler(show_supply_details, pattern="^supply_"))

    # Добавляем MessageHandler для кнопок из ReplyKeyboardMarkup
    application.add_handler(MessageHandler(filters.Regex("^Настройки мониторинга$"), handle_monitoring_settings))
    application.add_handler(MessageHandler(filters.Regex("^Запустить мониторинг$"), handle_start_monitoring))
    application.add_handler(MessageHandler(filters.Regex("^Остановить мониторинг$"), handle_stop_monitoring))
    application.add_handler(MessageHandler(filters.Regex("^Проверить поставки$"), show_supplies))

    # Добавляем MessageHandler для обработки пользовательского ввода
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    application.run_polling()

if __name__ == "__main__":
    main()
