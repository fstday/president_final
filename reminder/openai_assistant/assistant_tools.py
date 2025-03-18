import json
from typing import List, Dict, Any

# Определение функций для Assistant API

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "delete_reception_for_patient",
            "description": "Удаляет запись на прием для конкретного пациента",
            "parameters": {
                "type": "object",
                "properties": {
                    "patient_id": {
                        "type": "string",
                        "description": "Идентификатор пациента (patient_code)"
                    }
                },
                "required": ["patient_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "reserve_reception_for_patient",
            "description": "Создает или изменяет запись на прием для пациента на определенную дату и время",
            "parameters": {
                "type": "object",
                "properties": {
                    "patient_id": {
                        "type": "string",
                        "description": "Идентификатор пациента (patient_code)"
                    },
                    "date_from_patient": {
                        "type": "string",
                        "description": "Дата и время приема в формате YYYY-MM-DD HH:MM"
                    },
                    "trigger_id": {
                        "type": "integer",
                        "description": "ID триггера: 1 - стандартная запись, 2 - запись через Redis, 3 - проверка доступности",
                        "enum": [1, 2, 3]
                    }
                },
                "required": ["patient_id", "date_from_patient"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "appointment_time_for_patient",
            "description": "Получает информацию о текущей записи пациента",
            "parameters": {
                "type": "object",
                "properties": {
                    "patient_code": {
                        "type": "string",
                        "description": "Идентификатор пациента (patient_code)"
                    },
                    "year_from_patient_for_returning": {
                        "type": "string",
                        "description": "Дата и время в формате YYYY-MM-DD HH:MM (опционально)"
                    }
                },
                "required": ["patient_code"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "which_time_in_certain_day",
            "description": "Получает доступные времена для записи в определенный день",
            "parameters": {
                "type": "object",
                "properties": {
                    "reception_id": {
                        "type": "string",
                        "description": "Идентификатор пациента (patient_code)"
                    },
                    "date_time": {
                        "type": "string",
                        "description": "Дата в формате YYYY-MM-DD для проверки доступного времени"
                    }
                },
                "required": ["reception_id", "date_time"]
            }
        }
    }
]


# Функция для создания/обновления ассистента с инструментами
def create_assistant_with_tools(client, name: str, instructions: str, model: str = "gpt-4"):
    """
    Создает или обновляет ассистента с настроенными инструментами

    Args:
        client: OpenAI клиент
        name: Имя ассистента
        instructions: Инструкции для ассистента
        model: Модель ИИ для использования

    Returns:
        dict: Созданный или обновленный ассистент
    """
    # Базовые инструкции для ассистента
    base_instructions = f"""
    Ты помощник в медицинской системе, который обрабатывает естественно-языковые запросы пациентов 
    относительно их записей на прием к врачу.

    Ты можешь:
    1. Сообщать информацию о существующих записях к врачу
    2. Помогать с записью на прием
    3. Помогать с изменением или отменой записи на прием
    4. Узнавать доступные временные слоты для записи

    {instructions}

    Важные примечания:
    - Всегда уточняй намерения пациента, прежде чем вызывать функции
    - Не предполагай информацию, которую пациент не предоставил
    - При неуверенности запрашивай дополнительную информацию
    - Следуй формату ответа из документации для каждого действия
    """

    # Создаем или обновляем ассистента
    try:
        # Получаем список существующих ассистентов
        assistants = client.beta.assistants.list(limit=100)
        existing_assistant = None

        # Проверяем, есть ли ассистент с таким именем
        for assistant in assistants.data:
            if assistant.name == name:
                existing_assistant = assistant
                break

        if existing_assistant:
            # Обновляем существующего ассистента
            updated_assistant = client.beta.assistants.update(
                assistant_id=existing_assistant.id,
                name=name,
                instructions=base_instructions,
                model=model,
                tools=TOOLS
            )
            return updated_assistant
        else:
            # Создаем нового ассистента
            new_assistant = client.beta.assistants.create(
                name=name,
                instructions=base_instructions,
                model=model,
                tools=TOOLS
            )
            return new_assistant

    except Exception as e:
        raise Exception(f"Error creating/updating assistant: {str(e)}")


# Шаблоны ответов для различных статусов
RESPONSE_TEMPLATES = {
    # Успешные ответы для переноса записи
    "success_change_reception": {
        "status": "success_change_reception",
        "date": "{date}",  # например, "29 Января"
        "date_kz": "{date_kz}",  # например, "29 Қаңтар"
        "specialist_name": "{specialist_name}",
        "weekday": "{weekday}",  # например, "Пятница"
        "weekday_kz": "{weekday_kz}",  # например, "Жұма"
        "time": "{time}",  # например, "10:30"
    },

    # Доступно только одно время
    "only_first_time": {
        "status": "only_first_time",
        "date": "{date}",
        "date_kz": "{date_kz}",
        "specialist_name": "{specialist_name}",
        "weekday": "{weekday}",
        "weekday_kz": "{weekday_kz}",
        "first_time": "{first_time}",
    },

    # Нет доступных временных окон
    "error_empty_windows": {
        "status": "error_empty_windows",
        "message": "Свободных приемов не найдено."
    },

    # Успешное удаление записи
    "success_deleting_reception": {
        "status": "success_deleting_reception",
        "message": "Запись успешно удалена"
    },

    # Ошибка удаления записи
    "error_deleting_reception": {
        "status": "error_deleting_reception",
        "message": "{message}"
    }
}


def format_response(status_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Форматирует ответ в соответствии с требуемым форматом из документации

    Args:
        status_type: Тип статуса ответа
        data: Данные для включения в ответ

    Returns:
        dict: Отформатированный ответ
    """
    if status_type in RESPONSE_TEMPLATES:
        template = RESPONSE_TEMPLATES[status_type].copy()

        # Заполняем шаблон данными
        for key, value in template.items():
            if isinstance(value, str) and "{" in value and "}" in value:
                field_name = value.strip("{}")
                if field_name in data:
                    template[key] = data[field_name]

        # Добавляем дополнительные поля, если они есть в данных, но не в шаблоне
        for key, value in data.items():
            if key not in template:
                template[key] = value

        return template

    # Если шаблона нет, возвращаем данные как есть
    return data
