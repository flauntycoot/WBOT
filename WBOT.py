import json
import logging
import requests
import asyncio
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters

# Настройки логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Ключи для API Wildberries и Telegram
WB_API_KEY = "eyJhbGciOiJFUzI1NiIsImtpZCI6IjIwMjQxMTE4djEiLCJ0eXAiOiJKV1QifQ.eyJlbnQiOjEsImV4cCI6MTc0NzczMTIwMywiaWQiOiIwMTkzNDEwZC0zMTlkLTdjNDctYWJjYS05MDQyMTlmMTdmZmEiLCJpaWQiOjU4NjU2MzkzLCJvaWQiOjQyNTU1NzcsInMiOjEwNzM3NDI4NDgsInNpZCI6IjFmNTRhOGY5LWRlNTEtNDg5ZC04MWZiLWZlOTAwNDlkZmRmMSIsInQiOmZhbHNlLCJ1aWQiOjU4NjU2MzkzfQ.wQqeHP6gHpJz1Ytj4mh-EV3V8dEqnPmH0XZNOlmtJsO4L0dGPfPOeLsqUTv5qcIpRaBIgOQIhWciyLwsTys2Ow"
TELEGRAM_BOT_TOKEN = "8028999606:AAFYdlfmELLfQ388Y19ktkTrmUEfw-9XQAI"

# Глобальные переменные
monitored_warehouses = set()
selected_types = set()
monitoring_interval = 7  # Интервал мониторинга по умолчанию (в днях)
max_coefficient = 18  # Максимальный коэффициент по умолчанию
is_monitoring = False
previous_data = {}  # Хранит предыдущие результаты мониторинга
acceptance_type_emojis = {
    "Короба": "📦",
    "QR-поставка": "🏷️",
    "Монопаллеты": "🛠️",
    "Суперсейф": "🔒"
}


# Типы приемки
acceptance_types = ["Короба", "QR-поставка", "Монопаллеты", "Суперсейф"]

# Загрузка складов из файла
def load_warehouses():
    try:
        with open("warehouses.json", "r", encoding="utf-8") as file:
            return json.load(file)
    except FileNotFoundError:
        logger.error("Файл warehouses.json не найден.")
        return []
    except json.JSONDecodeError:
        logger.error("Ошибка чтения файла warehouses.json.")
        return []

all_warehouses = load_warehouses()

# Стартовая команда
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Пользователь начал взаимодействие с ботом.")
    keyboard = [
        [InlineKeyboardButton("Настройки мониторинга", callback_data="monitoring_settings")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Добро пожаловать! Используйте кнопки ниже для работы.", reply_markup=reply_markup)

# Получение данных из Wildberries
async def fetch_acceptance_coefficients():
    logger.info("Получение данных о коэффициентах из Wildberries API.")
    headers = {"Authorization": WB_API_KEY}
    try:
        response = requests.get("https://supplies-api.wildberries.ru/api/v1/acceptance/coefficients", headers=headers)
        response.raise_for_status()
        data = response.json()
        logger.info(f"Данные успешно получены: {len(data)} записей.")
        
        # Логирование первых нескольких записей для анализа
        for item in data[:10]:  # Логируем только первые 10 записей
            logger.debug(f"Запись: {item}")
            
        return data
    except requests.exceptions.RequestException as e:
        logger.error(f"Ошибка при получении данных от Wildberries API: {e}")
        return []


def is_similar(warehouse_api_name, warehouse_monitor_name, threshold=0.7):
    """
    Проверяет, схожи ли два названия складов, используя SequenceMatcher.
    :param warehouse_api_name: Название склада из API.
    :param warehouse_monitor_name: Название склада из списка отслеживаемых.
    :param threshold: Пороговое значение похожести (от 0 до 1).
    :return: True, если схожесть выше порогового значения.
    """
    similarity = SequenceMatcher(None, warehouse_api_name.lower(), warehouse_monitor_name.lower()).ratio()
    return similarity >= threshold


# Настройки мониторинга
async def monitoring_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Пользователь открыл настройки мониторинга.")
    keyboard = [
        [InlineKeyboardButton("Выбрать диапазон дат", callback_data="set_interval")],
        [InlineKeyboardButton("Изменить коэффициент", callback_data="set_coefficient")],
        [InlineKeyboardButton("Выбрать склады", callback_data="set_warehouses")],
        [InlineKeyboardButton("Выбрать тип приемки", callback_data="set_acceptance_types")],
        [InlineKeyboardButton("Запустить мониторинг", callback_data="start_monitoring")],
        [InlineKeyboardButton("Остановить мониторинг", callback_data="stop_monitoring")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    monitoring_message = (
        f"Текущие настройки:\n"
        f"Диапазон дат: {monitoring_interval} дней\n"
        f"Максимальный коэффициент: {max_coefficient}\n"
        f"Склады: {', '.join(monitored_warehouses) if monitored_warehouses else 'Все'}\n"
        f"Типы приемки: {', '.join(selected_types) if selected_types else 'Все'}"
    )
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(monitoring_message, reply_markup=reply_markup)
    else:
        await update.message.reply_text(monitoring_message, reply_markup=reply_markup)

# Установка диапазона мониторинга
async def set_interval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("24ч", callback_data="interval_1"), InlineKeyboardButton("2д", callback_data="interval_2")],
        [InlineKeyboardButton("3д", callback_data="interval_3"), InlineKeyboardButton("Неделя", callback_data="interval_7")],
        [InlineKeyboardButton("2 недели", callback_data="interval_14")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text("Выберите диапазон мониторинга:", reply_markup=reply_markup)

async def set_monitoring_interval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global monitoring_interval
    query = update.callback_query
    await query.answer()
    monitoring_interval = int(query.data.split("_")[1])
    logger.info(f"Установлен диапазон мониторинга: {monitoring_interval} дней.")
    await monitoring_settings(update, context)

    # Функция для выбора типов приемки
async def select_acceptance_types(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Пользователь начал выбор типов приемки.")
    query = update.callback_query
    await query.answer()

    # Список типов приемки с текущими настройками
    keyboard = [
        [InlineKeyboardButton(f"{acceptance} {'✅' if acceptance in selected_types else '❌'}", callback_data=f"toggle_type_{acceptance}")]
        for acceptance in acceptance_types
    ]
    keyboard.append([InlineKeyboardButton("Завершить выбор", callback_data="finish_acceptance_types")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text("Выберите типы приемки для мониторинга:", reply_markup=reply_markup)

    # Функция для переключения состояния типа приемки
async def toggle_acceptance_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    type_name = query.data.split("_")[2]
    if type_name in selected_types:
        selected_types.remove(type_name)
        logger.info(f"Тип приемки '{type_name}' удален из списка мониторинга.")
    else:
        selected_types.add(type_name)
        logger.info(f"Тип приемки '{type_name}' добавлен в список мониторинга.")

    await select_acceptance_types(update, context)

    # Завершение выбора типов приемки
async def finish_acceptance_types(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Пользователь завершил выбор типов приемки.")
    query = update.callback_query
    await query.answer()
    await monitoring_settings(update, context)

# Глобальные переменные
warehouses = load_warehouses()  # Все склады из файла
per_page = 5  # Количество складов на одной странице

# Разделим список складов на страницы
def paginate_warehouses(page):
    start = (page - 1) * per_page
    end = start + per_page
    return warehouses[start:end]


def generate_warehouse_id(warehouse: str) -> str:
    """
    Генерирует уникальный идентификатор для склада.
    Урезает длину и заменяет пробелы для совместимости с callback_data.
    """
    return str(abs(hash(warehouse)) % (10 ** 8))  # Хэш обрезается до 8 символов


# Обработчик для кнопки "Выбрать склады"
async def select_warehouses(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Пользователь начал выбор складов.")
    # Устанавливаем текущую страницу как 1
    context.user_data['current_page'] = 1
    # Передаем страницу в show_warehouses
    await show_warehouses(update, context, context.user_data['current_page'])

async def show_warehouses(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = None):
    logger.info(f"Отображаем страницу {page} складов.")
    
    if not page:
        callback_data = update.callback_query.data
        if callback_data.startswith("page_"):
            page = int(callback_data.split("_")[1])
        else:
            page = 1
    
    # Paginate warehouses
    total_pages = (len(all_warehouses) + per_page - 1) // per_page
    current_warehouses = paginate_warehouses(page)
    
    # Generate buttons for warehouses
    keyboard = [
        [
            InlineKeyboardButton(
                f"{warehouse} {'✅' if warehouse in monitored_warehouses else '❌'}",
                callback_data=f"toggle_{generate_warehouse_id(warehouse)}"
            )
        ]
        for warehouse in current_warehouses
    ]
    
    # Navigation buttons in one row
    navigation_buttons = []
    if page > 1:
        navigation_buttons.append(InlineKeyboardButton("<", callback_data=f"page_{page - 1}"))
    if page < total_pages:
        navigation_buttons.append(InlineKeyboardButton(">", callback_data=f"page_{page + 1}"))
    if page > 3:
        navigation_buttons.insert(0, InlineKeyboardButton("<<<", callback_data="page_1"))
    if page < total_pages - 2:
        navigation_buttons.append(InlineKeyboardButton(">>>", callback_data=f"page_{total_pages}"))
    
    if navigation_buttons:
        keyboard.append(navigation_buttons)
    
    # Add "Finish selection" button
    keyboard.append([InlineKeyboardButton("Завершить выбор", callback_data="finish_warehouses")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.callback_query.edit_message_text(
        f"Выберите склады (страница {page}/{total_pages}):",
        reply_markup=reply_markup
    )




# Обработчик навигации по страницам
async def page_navigation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # Получаем номер текущей страницы из callback_data
    page = int(query.data.split("_")[1])
    # Сохраняем номер страницы в контексте
    context.user_data['current_page'] = page
    # Передаем страницу в функцию show_warehouses
    await show_warehouses(update, context, page)



# Сохранение текущей страницы
async def set_current_page(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int) -> None:
    context.user_data["current_page"] = page



async def toggle_warehouse(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Переключение состояния склада.")
    query = update.callback_query
    await query.answer()

    warehouse_id = query.data.split("_")[1]
    warehouse_name = next((w for w in all_warehouses if generate_warehouse_id(w) == warehouse_id), None)

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




# Функция для завершения выбора складов
async def finish_warehouses(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("Пользователь завершил выбор складов.")
    query = update.callback_query
    await query.answer()

    # Переход обратно к настройкам мониторинга
    await monitoring_settings(update, context)



# Изменение коэффициента
async def set_monitoring_coefficient(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    logger.info("Пользователь выбрал изменить коэффициент.")
    await query.edit_message_text("Введите максимальный коэффициент (0 = бесплатно):")
    context.user_data["awaiting_coefficient"] = True

async def handle_coefficient_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.get("awaiting_coefficient"):
        try:
            global max_coefficient
            max_coefficient = int(update.message.text)
            context.user_data["awaiting_coefficient"] = False
            logger.info(f"Установлен максимальный коэффициент: {max_coefficient}.")
            await update.message.reply_text(f"Максимальный коэффициент установлен: {max_coefficient}")
            await monitoring_settings(update, context)
        except ValueError:
            await update.message.reply_text("Ошибка: введите целое число.")





def format_coefficients(data):
    """
    Форматирует данные для вывода в читаемом виде с типом приемки (emoji).
    :param data: Список записей.
    :return: Отформатированный список строк.
    """
    logger.info("Форматирование данных для отправки.")
    formatted = []
    for item in data:
        # Получаем склад, дату и коэффициент
        warehouse = item.get("warehouseName", "Неизвестный склад")
        date = datetime.fromisoformat(item["date"]).strftime("%d-%m")
        coefficient = item.get("coefficient", -1)
        
        # Получаем тип приемки и его emoji
        box_type = item.get("boxTypeName", "Неизвестный тип")
        box_emoji = acceptance_type_emojis.get(box_type, "❓")

        # Устанавливаем статус на основе коэффициента
        if coefficient == 0:
            status = "🟩 FREE"
        elif coefficient <= 10:
            status = f"🟦 {coefficient}"
        else:
            status = f"🟧 {coefficient}"

        # Добавляем форматированную строку
        formatted.append(f"{box_emoji} [{status}] [{warehouse}] [{date}] ")
    logger.info(f"Форматирование завершено. Подготовлено {len(formatted)} записей.")
    return formatted




def filter_coefficients(data, interval, max_coeff):
    """
    Фильтрует данные по складам, диапазону дат, коэффициентам и типу приемки.
    :param data: Данные, полученные из API.
    :param interval: Диапазон мониторинга (в днях).
    :param max_coeff: Максимальный допустимый коэффициент.
    :return: Список отфильтрованных записей.
    """
    logger.info("Фильтрация данных по складам, диапазону дат, коэффициентам и типу приемки.")
    logger.info(f"Активные склады для мониторинга: {monitored_warehouses}")

    filtered = []
    end_date = datetime.now() + timedelta(days=interval)

    for item in data:
        warehouse = item.get("warehouseName", "Неизвестный склад")
        date = datetime.fromisoformat(item["date"][:10])
        coefficient = item.get("coefficient", None)
        box_type = item.get("boxTypeName", "")

        # Исключаем некорректные коэффициенты
        if coefficient is None or coefficient < 0:
            continue

        # Фильтрация по складам, типам приемки, диапазону дат и коэффициентам
        if warehouse in monitored_warehouses and box_type in selected_types:
            if date <= end_date and coefficient <= max_coeff:
                filtered.append(item)

    logger.info(f"Отфильтровано {len(filtered)} записей из {len(data)}.")
    return filtered





async def start_monitoring(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global is_monitoring, previous_data
    query = update.callback_query
    await query.answer()
    is_monitoring = True
    await query.edit_message_text("Мониторинг запущен!")

    # Для ограничения логов
    log_interval = 40  # Интервал логирования
    last_log_time = datetime.now()

    while is_monitoring:
        logger.info("Запрос данных для мониторинга.")
        coefficients = await fetch_acceptance_coefficients()

        if coefficients:
            filtered = filter_coefficients(coefficients, monitoring_interval, max_coefficient)
            new_data = {f"{item['warehouseName']}-{item['date']}": item for item in filtered}
            new_items = [item for key, item in new_data.items() if key not in previous_data]

            if new_items:
                logger.info(f"Обнаружено {len(new_items)} новых записей.")
                await query.message.reply_text("\n".join(format_coefficients(new_items)))
            previous_data = new_data

        # Логирование через заданный интервал
        if (datetime.now() - last_log_time).seconds >= log_interval:
            logger.info(f"Мониторинг активен. Проверено {len(coefficients)} записей.")
            last_log_time = datetime.now()

        await asyncio.sleep(10)  # Период запроса к API



# Остановка мониторинга
async def stop_monitoring(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global is_monitoring
    query = update.callback_query
    await query.answer()
    is_monitoring = False
    await query.edit_message_text("Мониторинг остановлен.")

# Запуск приложения
def main():
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_coefficient_input))
    application.add_handler(CallbackQueryHandler(monitoring_settings, pattern="^monitoring_settings$"))
    application.add_handler(CallbackQueryHandler(set_interval, pattern="^set_interval$"))
    application.add_handler(CallbackQueryHandler(set_monitoring_interval, pattern="^interval_"))
    application.add_handler(CallbackQueryHandler(set_monitoring_coefficient, pattern="^set_coefficient$"))
    application.add_handler(CallbackQueryHandler(start_monitoring, pattern="^start_monitoring$"))
    application.add_handler(CallbackQueryHandler(stop_monitoring, pattern="^stop_monitoring$"))
    application.add_handler(CallbackQueryHandler(monitoring_settings, pattern="^monitoring_settings$"))
    application.add_handler(CallbackQueryHandler(select_acceptance_types, pattern="^set_acceptance_types$"))
    application.add_handler(CallbackQueryHandler(toggle_acceptance_type, pattern="^toggle_type_"))
    application.add_handler(CallbackQueryHandler(finish_acceptance_types, pattern="^finish_acceptance_types$"))
    application.add_handler(CallbackQueryHandler(toggle_warehouse, pattern="^toggle_"))
    application.add_handler(CallbackQueryHandler(show_warehouses, pattern="^set_warehouses$"))
    application.add_handler(CallbackQueryHandler(page_navigation, pattern="^page_"))
    application.add_handler(CallbackQueryHandler(finish_warehouses, pattern="^finish_warehouses$"))
    



    application.run_polling()

if __name__ == "__main__":
    main()
