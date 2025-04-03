import json
import logging
from datetime import datetime, timedelta
from pydantic import BaseModel, Field
from openai import OpenAI
from django.conf import settings


class DateTimeExtraction(BaseModel):
    """Модель для извлечения даты и времени из пользовательского запроса"""
    date: str = Field(description="Дата в формате YYYY-MM-DD")
    time: str = Field(description="Время в формате HH:MM")
    relative_day: str = Field(
        description="Относительный день (сегодня, завтра, послезавтра, через_2_дня, через_неделю)")
    time_of_day: str = Field(description="Время суток (утро, день, вечер)")


def extract_date_from_input(user_input, client=None, reception_start_time=None, current_datetime_str=None):
    """
    Извлекает дату, время и контекстную информацию из пользовательского ввода

    Args:
        user_input: Текст от пользователя
        client: Клиент OpenAI
        reception_start_time: Текущее время записи пациента (для переносов)
        current_datetime_str: Текущая дата и время в строковом формате

    Returns:
        dict: Извлеченная дата, время и контекстная информация о запросе
    """
    import json
    from datetime import datetime, timedelta

    if client is None:
        from openai import OpenAI
        client = OpenAI(api_key=settings.OPENAI_API_KEY)

    # Текущая дата и время
    now = datetime.now()
    if current_datetime_str:
        try:
            now = datetime.strptime(current_datetime_str, '%Y-%m-%d %H:%M:%S')
        except ValueError:
            # Если формат не соответствует, используем текущую дату/время
            pass

    # Инструкции для извлечения даты/времени
    system_prompt = f"""
    Ты помощник, который извлекает дату, время и намерения из текста на русском языке.
    Твоя задача - правильно интерпретировать запросы пациентов относительно записи на прием.

    ВАЖНЫЕ ПРАВИЛА ИНТЕРПРЕТАЦИИ ДАТЫ:
    1. "сегодня" = текущий день: {now.strftime('%Y-%m-%d')}
    2. "завтра" = текущий день + 1 день: {(now + timedelta(days=1)).strftime('%Y-%m-%d')}
    3. "послезавтра" = текущий день + 2 дня: {(now + timedelta(days=2)).strftime('%Y-%m-%d')}
    4. "после послезавтра" = текущий день + 3 дня: {(now + timedelta(days=3)).strftime('%Y-%m-%d')}
    5. "через неделю" = текущий день + 7 дней: {(now + timedelta(days=7)).strftime('%Y-%m-%d')}

    ПРАВИЛА ВРЕМЕНИ СУТОК:
    - "утро" = 10:30
    - "день" или "обед" = 13:30
    - "вечер" = 18:30
    - "после обеда" = время после 13:00 (обычно 14:00)
    - "вечер" или "вечернее время" = время после 16:00

    ПРАВИЛА ОПРЕДЕЛЕНИЯ ДЕЙСТВИЙ:
    - "записать", "запиши", "поставь", "закрепи" = действие 'reserve' (создание новой записи)
    - "перенести", "перенесите", "переоформите" = действие 'reschedule' (если указана конкретная дата или время) или 'reschedule_day' (если указан только день)
    - "раньше" без указания конкретного времени = действие 'earlier', выбрать время до {reception_start_time} в тот же день
    - "позже", "попозже" без указания конкретного времени = действие 'later', выбрать время после {reception_start_time} в тот же день
    - "удалить", "отменить", "отмените", "убрать", "отказаться", "не хочу", "перестаньте", "уберите запись", "исключить", "закрыть", "отказ", "не актуально" = действие 'delete'
    - "ближайшее свободное время" = действие 'earliest_available'
    - запрос на конкретную дату и время = действие 'reserve'

    РЕШАЮЩИЕ ПРАВИЛА ДЛЯ ОТМЕНЫ/ПЕРЕНОСА:
    - Если в сообщении содержатся слова "перенеси", "перенесите", "переоформите", "запиши", "записать" - это ВСЕГДА запрос на перенос или создание записи, а НЕ на удаление.
    - Запрос на удаление возможен только если используются слова "удалить", "удалите", "отменить", "отмените" и т.д. без упоминания создания новой записи.

    ДОПОЛНИТЕЛЬНЫЕ КОНТЕКСТНЫЕ ПРАВИЛА:
    1. Если пациент просит "ближайшее свободное время" - выбираем самое раннее доступное время.
    2. Если пациент говорит "удобнее после обеда" - выбираем время после 13:00.
    3. Если пациент хочет время, "когда меньше людей" - выбираем время ближе к началу (9:00-10:00) или концу рабочего дня (17:00-18:00).
    4. Если пациент хочет перенести запись "на раньше" - выбираем время только до текущего времени записи: {reception_start_time}.
    5. Если пациент хочет перенести запись "на позже" - выбираем время только после текущего времени записи: {reception_start_time}.
    6. Если пациент просит "перенести на вечер" - выбираем время после 16:00.
    7. Если пациент просит перенести куда-то без уточнения дня - предполагаем тот же день, что и текущая запись.
    8. Если пациент говорит "перенесите на послезавтра" - это 'reschedule_day' на дату через 2 дня от текущей даты, независимо от даты текущей записи.
    9. "Перенесите на сегодня" — действие 'reschedule_day', дата — сегодняшняя дата.
    10. "Перенеси на завтра" — действие 'reschedule_day', дата — завтрашняя дата.

    ПРИМЕРЫ СИТУАЦИЙ:
    1. Пациент просит перенести запись на ближайшее свободное время — выберите время, которое доступно раньше всех. 
    2. Пациенту удобнее после обеда — выберите время после 13:00, если доступно. 
    3. Пациент говорит, что хочет время, когда будет меньше людей — выберите время, максимально близкое к началу или концу рабочего дня. 
    4. Пациент говорит, что хочет перенести запись на раньше - не рассматриваем время, которое позже времени текущей записи пациента: {reception_start_time}. 
    5. Пациент говорит, что хочет перенести запись на позже - рассматриваем только время, которое позже времени текущей записи пациента: {reception_start_time}. 
    6. Когда пациент говорит, что хочет перенести запись раньше или позже - выберите время, в день когда существует нынешняя запись у пациента: {reception_start_time}. 
    7. Пациенту нужно перенести запись на раньше или позже без уточнения дня - выберите время в день когда существует нынешняя запись у пациента. 
    8. Пациенту нужно перенести запись на вечер - выберите время после 16:00, если доступно. 
    9. Пациент говорит: 'Перенесите запись на после завтра' - интерпретация: действие 'reschedule_day', дата и время - на 2 дня вперед, чем дата сегодня: ({current_datetime_str}). Не учитывайте дату записи, а отталкивайтесь от даты сегодня.
    10. Когда пациент говорит что нужно перенести запись позже или попозже, не удаляй его запись если не получилось записать на свободные времена.

    ВОЗВРАЩАЕМЫЙ ФОРМАТ:
    Возвращай результат в JSON формате с полями:
    - date: дата в формате YYYY-MM-DD
    - time: время в формате HH:MM
    - relative_day: относительный день (сегодня, завтра, послезавтра, и т.д.)
    - time_of_day: время суток (утро, день, вечер)
    - action: определенное действие (reserve, reschedule, reschedule_day, earlier, later, delete, earliest_available)
    - context: дополнительная информация о контексте запроса
    - description: краткое описание намерения пациента (например, "Пользователь хочет перенести запись на завтра утром")
    """

    # Отправляем запрос к API
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",
             "content": f"Извлеки дату, время и контекст из следующего запроса: '{user_input}'. Текущая дата: {now.strftime('%Y-%m-%d')}. Время текущей записи пациента (если есть): {reception_start_time}."}
        ]
    )

    # Парсим ответ
    result = response.choices[0].message.content
    extracted_data = json.loads(result)

    # Обработка относительных дат
    if extracted_data.get("relative_day"):
        relative_day = extracted_data["relative_day"]

        if relative_day == "сегодня":
            extracted_data["date"] = now.strftime('%Y-%m-%d')
        elif relative_day == "завтра":
            extracted_data["date"] = (now + timedelta(days=1)).strftime('%Y-%m-%d')
        elif relative_day == "послезавтра":
            extracted_data["date"] = (now + timedelta(days=2)).strftime('%Y-%m-%d')
        elif relative_day in ["после_послезавтра", "после послезавтра"]:
            extracted_data["date"] = (now + timedelta(days=3)).strftime('%Y-%m-%d')
        elif relative_day in ["через_неделю", "через неделю"]:
            extracted_data["date"] = (now + timedelta(days=7)).strftime('%Y-%m-%d')

    # Обработка времени суток
    if extracted_data.get("time_of_day") and not extracted_data.get("time"):
        time_of_day = extracted_data["time_of_day"]

        if time_of_day == "утро":
            extracted_data["time"] = "10:30"
        elif time_of_day in ["день", "обед"]:
            extracted_data["time"] = "13:30"
        elif time_of_day == "вечер":
            extracted_data["time"] = "18:30"
        elif time_of_day == "после обеда":
            extracted_data["time"] = "14:00"

    # Если время все еще не указано, используем дефолтное значение
    if not extracted_data.get("time"):
        # Для различных действий могут быть разные дефолтные значения
        if extracted_data.get("action") == "earlier":
            # Если есть текущее время записи, выбираем время на час раньше
            if reception_start_time:
                try:
                    # Парсим время из строки
                    reception_time = datetime.strptime(reception_start_time, "%H:%M").time()
                    # Вычитаем 1 час
                    reception_hour = reception_time.hour - 1
                    if reception_hour < 8:  # Минимально возможное время (начало рабочего дня)
                        reception_hour = 8
                    extracted_data["time"] = f"{reception_hour:02d}:{reception_time.minute:02d}"
                except (ValueError, TypeError):
                    extracted_data["time"] = "09:30"  # Предполагаем более раннее время
        elif extracted_data.get("action") == "later":
            # Если есть текущее время записи, выбираем время на час позже
            if reception_start_time:
                try:
                    # Парсим время из строки
                    reception_time = datetime.strptime(reception_start_time, "%H:%M").time()
                    # Добавляем 1 час
                    reception_hour = reception_time.hour + 1
                    if reception_hour > 20:  # Максимально возможное время (конец рабочего дня)
                        reception_hour = 20
                    extracted_data["time"] = f"{reception_hour:02d}:{reception_time.minute:02d}"
                except (ValueError, TypeError):
                    extracted_data["time"] = "14:30"  # Предполагаем более позднее время
        else:
            extracted_data["time"] = "10:30"  # дефолтное время - утро

    # Дополнительные проверки контекста
    lower_input = user_input.lower()

    # Обработка "меньше людей"
    if "меньше людей" in lower_input:
        extracted_data["context"] = "предпочтение для времени с меньшей загруженностью"
        # Выбираем утреннее или вечернее время
        extracted_data["time"] = "09:30" if "утр" in lower_input else "17:30"

    # Обработка "ближайшее свободное время"
    if "ближайшее" in lower_input and "свободное" in lower_input:
        extracted_data["action"] = "earliest_available"
        extracted_data["context"] = "запрос на ближайшее свободное время"

    # Проверка на перенос "на раньше" или "на позже"
    if reception_start_time and ("раньше" in lower_input or "позже" in lower_input or "попозже" in lower_input):
        if "раньше" in lower_input:
            extracted_data["action"] = "earlier"
            extracted_data["context"] = "запрос на более раннее время в тот же день"
        elif "позже" in lower_input or "попозже" in lower_input:
            extracted_data["action"] = "later"
            extracted_data["context"] = "запрос на более позднее время в тот же день"

    # Важная логика для отмены/переноса
    # Сначала проверим наличие ключевых слов переноса/записи
    reschedule_keywords = [
        "перенеси", "перенесите", "переоформите", "запиши", "запишите",
        "записать", "поставь", "поставьте", "закрепи", "закрепите"
    ]
    has_reschedule_intent = any(keyword in lower_input for keyword in reschedule_keywords)

    # Затем проверим наличие ключевых слов удаления
    delete_keywords = [
        "удалить", "отменить", "отмените", "убрать", "отказаться", "не хочу",
        "перестаньте", "уберите запись", "исключить", "закрыть", "отказ",
        "не актуально", "больше не нужно", "не требуется"
    ]
    has_delete_intent = any(keyword in lower_input for keyword in delete_keywords)

    # Если есть намерение переноса, то это не удаление, даже если есть слова удаления
    if has_reschedule_intent:
        # Сбрасываем действие удаления, если оно было установлено
        if extracted_data.get("action") == "delete":
            if "date" in extracted_data and "time" in extracted_data:
                extracted_data["action"] = "reschedule"
            else:
                extracted_data["action"] = "reschedule_day"
            extracted_data["context"] = "запрос на перенос записи"
    # Если есть только намерение удаления (без намерения переноса)
    elif has_delete_intent:
        extracted_data["action"] = "delete"
        extracted_data["context"] = "запрос на удаление/отмену записи"

    # Если не указано действие, но есть дата и время, то это запрос на запись
    if not extracted_data.get("action") and extracted_data.get("date") and extracted_data.get("time"):
        extracted_data["action"] = "reserve"
        extracted_data["context"] = "запрос на запись на конкретное время"

    return extracted_data
