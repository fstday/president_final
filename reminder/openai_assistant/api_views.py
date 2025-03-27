import json
import logging
import re
from datetime import datetime, timezone, timedelta

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.conf import settings
from django.utils import timezone

from reminder.models import Patient, Appointment, Assistant, Thread, Run, IgnoredPatient
from reminder.openai_assistant.assistant_instructions import get_enhanced_assistant_prompt
from reminder.openai_assistant.assistant_tools import format_response

logger = logging.getLogger(__name__)

# Import the updated AssistantClient
from reminder.openai_assistant.assistant_client import AssistantClient


@csrf_exempt
@require_http_methods(["POST"])
def process_voicebot_request(request):
    """
    Улучшенная функция обработки запросов от голосового робота ACS.

    Ожидает JSON с полями:
    - appointment_id: ID записи на прием
    - user_input: Текст от пользователя

    Возвращает:
    - JSON с форматированным ответом согласно документации
    """
    try:
        # Разбор данных запроса
        data = json.loads(request.body)
        appointment_id = data.get('appointment_id')
        user_input = data.get('user_input')

        logger.info(f"\n\n=================================================\n\n"
                    f"Начало запроса: "
                    f"appointment_id={appointment_id}, "
                    f"user_input='{user_input}'"
                    f"\n\n=================================================\n\n")

        if not appointment_id or not user_input:
            logger.warning("Отсутствуют обязательные параметры")
            return JsonResponse({
                'status': 'error_bad_input',
                'message': 'Отсутствуют обязательные параметры: appointment_id и user_input'
            }, status=400)

        # Проверяем существование записи
        try:
            appointment = Appointment.objects.get(appointment_id=appointment_id)
        except Appointment.DoesNotExist:
            logger.error(f"Запись {appointment_id} не найдена")
            return JsonResponse({
                'status': 'error_reception_unavailable',
                'message': 'Запись не активна или не найдена'
            }, status=404)

        # Проверяем, находится ли пациент в списке игнорируемых
        if IgnoredPatient.objects.filter(patient_code=appointment.patient.patient_code).exists():
            logger.warning(f"Пациент {appointment.patient.patient_code} находится в списке игнорируемых")
            return JsonResponse({
                'status': 'error_ignored_patient',
                'message': f'Пациент с кодом {appointment.patient.patient_code} находится в списке игнорируемых.'
            }, status=403)

        # Предварительная обработка запроса пользователя
        user_input = preprocess_user_input(user_input)

        # Прямое определение и вызов функций для специфических запросов
        direct_function_result = try_direct_function_call(user_input, appointment)
        if direct_function_result:
            logger.info(f"Прямой вызов функции выполнен успешно, возвращаем результат")
            return JsonResponse(direct_function_result)

        # Инициализируем клиент ассистента
        assistant_client = AssistantClient()

        # Получаем или создаем тред для диалога
        thread = assistant_client.get_or_create_thread(appointment_id)

        # Если есть незавершенный запуск, ждем его завершения
        if thread.current_run and thread.current_run.status not in ['completed', 'failed', 'cancelled', 'expired']:
            logger.info(f"Ожидаем завершения предыдущего запуска {thread.current_run.run_id}")
            run_status = assistant_client.wait_for_run_completion(
                thread_id=thread.thread_id,
                run_id=thread.current_run.run_id
            )
            logger.info(f"Предыдущий запуск завершен со статусом: {run_status}")

        # Добавляем сообщение пользователя в тред
        assistant_client.add_message_to_thread(thread.thread_id, user_input)

        # Запускаем ассистента
        logger.info(f"Запускаем нового ассистента для треда {thread.thread_id}")
        run = assistant_client.run_assistant(thread, appointment)

        # Ждем завершения запуска
        run_status = assistant_client.wait_for_run_completion(
            thread_id=thread.thread_id,
            run_id=run.run_id,
            timeout=60  # 60 секунд таймаут
        )
        logger.info(f"Запуск ассистента завершен со статусом: {run_status}")

        # Получаем последнее сообщение ассистента
        messages = assistant_client.get_messages(thread.thread_id, limit=1)
        if not messages:
            logger.error("Нет ответа от ассистента")
            return JsonResponse({
                'status': 'error',
                'message': 'Не удалось получить ответ от ассистента'
            }, status=500)

        # Извлекаем текст ответа
        assistant_message = messages[0]
        response_text = ""

        # OpenAI может возвращать контент в разных форматах
        if assistant_message.content:
            for content_part in assistant_message.content:
                if content_part.type == 'text':
                    response_text += content_part.text.value

        logger.info(f"Сырой ответ ассистента: {response_text[:500]}...")  # Логируем первые 500 символов

        # Пытаемся найти и извлечь JSON объект из текста
        try:
            # Используем регулярное выражение для поиска JSON объекта в тексте
            json_match = re.search(r'\{.*\}', response_text, re.DOTALL)

            if json_match:
                json_str = json_match.group(0)
                response_data = json.loads(json_str)

                # Если в ответе есть поле status, это валидный ответ
                if 'status' in response_data:
                    # Форматируем ответ согласно документации
                    formatted_response = format_response(response_data['status'], response_data)
                    logger.info(f"Возвращаем форматированный ответ: {formatted_response}")
                    return JsonResponse(formatted_response)

            # Специальная обработка запросов о доступных временных слотах
            if "свободные окошки" in user_input.lower() or "когда можно записаться" in user_input.lower():
                # Если ассистент не вызвал правильную функцию, вызываем ее напрямую
                logger.warning("Ассистент не вызвал функцию which_time_in_certain_day, вызываем ее напрямую")

                from reminder.infoclinica_requests.schedule.which_time_in_certain_day import which_time_in_certain_day

                # Определяем, для какого дня нужны слоты
                today = datetime.now().strftime("%Y-%m-%d")
                response = which_time_in_certain_day(appointment.patient.patient_code, today)

                # Преобразуем JsonResponse в dict при необходимости
                if hasattr(response, 'content'):
                    return response
                else:
                    return JsonResponse(response)

            # Если нет JSON и нет специальной обработки, возвращаем текстовый ответ
            logger.info("JSON не найден в ответе, возвращаем текст")
            return JsonResponse({
                'status': 'assistant_response',
                'message': response_text.strip()
            })

        except Exception as e:
            logger.error(f"Ошибка при обработке ответа ассистента: {e}", exc_info=True)
            # В случае ошибки разбора JSON, возвращаем текстовый ответ
            return JsonResponse({
                'status': 'assistant_response',
                'message': response_text.strip()
            })

    except json.JSONDecodeError:
        logger.error("Неверный формат JSON в запросе")
        return JsonResponse({
            'status': 'error_bad_input',
            'message': 'Неверный формат JSON'
        }, status=400)
    except Exception as e:
        logger.error(f"Ошибка обработки запроса: {e}", exc_info=True)
        return JsonResponse({
            'status': 'error',
            'message': f'Ошибка обработки запроса: {str(e)}'
        }, status=500)


def create_assistant_with_tools(client, name: str, instructions: str, model: str = "gpt-4"):
    """
    Создает или обновляет ассистента с инструментами (tools).
    """
    if instructions is None:
        instructions = get_enhanced_assistant_prompt()
    TOOLS = [
        {
            "type": "function",
            "function": {
                "name": "which_time_in_certain_day",
                "description": "Получение доступного времени на конкретный день",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reception_id": {"type": "string", "description": "ID приема"},
                        "date_time": {"type": "string", "description": "Дата YYYY-MM-DD"}
                    },
                    "required": ["reception_id", "date_time"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "appointment_time_for_patient",
                "description": "Получение текущей записи пациента",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "patient_code": {"type": "string", "description": "Код пациента"}
                    },
                    "required": ["patient_code"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "reserve_reception_for_patient",
                "description": "Запись или перенос приема",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "patient_id": {"type": "string", "description": "ID пациента"},
                        "date_from_patient": {"type": "string", "description": "Дата приема YYYY-MM-DD HH:MM"},
                        "trigger_id": {"type": "integer", "description": "1 - запись, 2 - перенос"}
                    },
                    "required": ["patient_id", "date_from_patient", "trigger_id"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "delete_reception_for_patient",
                "description": "Отмена записи пациента",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "patient_id": {"type": "string", "description": "ID пациента"}
                    },
                    "required": ["patient_id"]
                }
            }
        }
    ]

    try:
        assistants = client.beta.assistants.list(limit=100)
        existing_assistant = None

        for assistant in assistants.data:
            if assistant.name == name:
                existing_assistant = assistant
                break

        if existing_assistant:
            logger.info(f"🔄 Обновление ассистента {existing_assistant.id}...")
            updated_assistant = client.beta.assistants.update(
                assistant_id=existing_assistant.id,
                name=name,
                instructions=instructions,
                model=model,
                tools=TOOLS
            )
            return updated_assistant
        else:
            logger.info("🆕 Создание нового ассистента...")
            new_assistant = client.beta.assistants.create(
                name=name,
                instructions=instructions,
                model=model,
                tools=TOOLS
            )
            return new_assistant

    except Exception as e:
        logger.error(f"❌ Ошибка создания/обновления ассистента: {e}")
        raise


@csrf_exempt
@require_http_methods(["GET"])
def get_assistant_info(request):
    """
    Возвращает информацию о сохраненных ассистентах
    """
    try:
        assistants = Assistant.objects.all()
        assistants_data = [{
            'id': assistant.id,
            'assistant_id': assistant.assistant_id,
            'name': assistant.name,
            'model': assistant.model,
            'created_at': assistant.created_at.isoformat()
        } for assistant in assistants]

        return JsonResponse({
            'status': 'success',
            'assistants': assistants_data
        })
    except Exception as e:
        logger.error(f"Error getting assistants: {e}", exc_info=True)
        return JsonResponse({
            'status': 'error',
            'message': f'Ошибка получения информации об ассистентах: {str(e)}'
        }, status=500)


def preprocess_user_input(text: str) -> str:
    """
    Предварительная обработка текста запроса пользователя.

    Args:
        text: Текст запроса пользователя

    Returns:
        str: Обработанный текст
    """
    # Удаляем лишние пробелы
    text = text.strip()

    # Нормализуем упоминания дат
    text = text.lower().replace('сегодняшний день', 'сегодня')
    text = text.replace('завтрашний день', 'завтра')
    text = text.replace('следующий день', 'завтра')

    # Нормализуем упоминания времени суток
    time_replacements = {
        'в утреннее время': 'утром',
        'ранним утром': 'утром',
        'с утра пораньше': 'утром',
        'в обеденное время': 'в обед',
        'во время обеда': 'в обед',
        'ближе к обеду': 'в обед',
        'вечернее время': 'вечером',
        'поздним вечером': 'вечером',
        'ближе к вечеру': 'вечером'
    }

    for original, replacement in time_replacements.items():
        text = text.replace(original, replacement)

    return text


def try_direct_function_call(user_input: str, appointment) -> dict:
    """
    Пытается напрямую определить и вызвать нужную функцию для определенных типов запросов.

    Args:
        user_input: Запрос пользователя
        appointment: Объект записи на прием

    Returns:
        dict: Результат вызова функции или None, если прямой вызов невозможен
    """
    user_input = user_input.lower()
    patient_code = appointment.patient.patient_code

    # Импортируем функции
    from reminder.infoclinica_requests.schedule.which_time_in_certain_day import which_time_in_certain_day
    from reminder.infoclinica_requests.schedule.appointment_time_for_patient import appointment_time_for_patient
    from reminder.infoclinica_requests.schedule.reserve_reception_for_patient import reserve_reception_for_patient
    from reminder.infoclinica_requests.schedule.delete_reception_for_patient import delete_reception_for_patient

    # 1. Запрос текущей записи
    if any(phrase in user_input for phrase in [
        'когда у меня запись', 'на какое время я записан', 'когда мой прием',
        'на какое время моя запись', 'когда мне приходить'
    ]):
        logger.info("Прямой вызов функции appointment_time_for_patient")
        result = appointment_time_for_patient(patient_code)
        # Преобразуем JsonResponse в dict при необходимости
        if hasattr(result, 'content'):
            return json.loads(result.content.decode('utf-8'))
        return result

    # 2. Запрос на отмену записи
    if any(phrase in user_input for phrase in [
        'отмени', 'отменить', 'удали', 'удалить', 'убрать запись',
        'не хочу приходить', 'отказаться от записи'
    ]):
        logger.info("Прямой вызов функции delete_reception_for_patient")
        result = delete_reception_for_patient(patient_code)
        # Преобразуем JsonResponse в dict при необходимости
        if hasattr(result, 'content'):
            return json.loads(result.content.decode('utf-8'))
        return result

    # 3. Запрос доступных времен
    if any(phrase in user_input for phrase in [
        'свободные окошки', 'доступное время', 'какие времена', 'когда можно записаться',
        'доступные времена', 'свободное время', 'когда свободно'
    ]):
        logger.info("Прямой вызов функции which_time_in_certain_day")

        # Определяем, для какой даты нужны слоты
        if 'завтра' in user_input:
            date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        elif 'сегодня' in user_input:
            date = datetime.now().strftime("%Y-%m-%d")
        else:
            # По умолчанию - сегодня
            date = datetime.now().strftime("%Y-%m-%d")

        result = which_time_in_certain_day(patient_code, date)
        # Преобразуем JsonResponse в dict при необходимости
        if hasattr(result, 'content'):
            return json.loads(result.content.decode('utf-8'))
        return result

    # В остальных случаях используем ассистента
    return None
