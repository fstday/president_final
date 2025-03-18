import json
import logging
from datetime import datetime

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.conf import settings
from django.utils import timezone

from reminder.models import Patient, Appointment, Assistant, Thread, Run
from reminder.openai_assistant.assistant_client import AssistantClient
from reminder.openai_assistant.assistant_tools import format_response

logger = logging.getLogger(__name__)


@csrf_exempt
@require_http_methods(["POST"])
def process_voicebot_request(request):
    """
    Обрабатывает запрос от голосового робота ACS.

    Ожидает JSON с полями:
    - appointment_id: ID записи на прием
    - user_input: Входящий текст от пользователя

    Возвращает:
    - JSON с отформатированным ответом в соответствии с документацией
    """
    try:
        data = json.loads(request.body)
        appointment_id = data.get('appointment_id')
        user_input = data.get('user_input')

        if not appointment_id or not user_input:
            return JsonResponse({
                'status': 'error_bad_input',
                'message': 'Отсутствуют обязательные параметры: appointment_id и user_input'
            }, status=400)

        # Проверяем, существует ли запись на прием
        try:
            appointment = Appointment.objects.get(appointment_id=appointment_id)
        except Appointment.DoesNotExist:
            return JsonResponse({
                'status': 'error_reception_unavailable',
                'message': 'Прием не активен или не найден'
            }, status=404)

        # Проверяем, не находится ли пациент в списке игнорируемых
        from reminder.models import IgnoredPatient
        if IgnoredPatient.objects.filter(patient_code=appointment.patient.patient_code).exists():
            return JsonResponse({
                'status': 'error_ignored_patient',
                'message': f'Пациент с кодом {appointment.patient.patient_code} находится в списке игнорируемых.'
            }, status=403)

        # Инициализируем клиент ассистента
        assistant_client = AssistantClient()

        # Получаем или создаем поток для диалога
        thread = assistant_client.get_or_create_thread(appointment_id)

        # Если есть запущенный процесс, который не завершен, ждем его завершения
        if thread.current_run and thread.current_run.status not in ['completed', 'failed', 'cancelled', 'expired']:
            run_status = assistant_client.wait_for_run_completion(
                thread_id=thread.thread_id,
                run_id=thread.current_run.run_id
            )
            logger.info(f"Waiting for previous run to complete. Status: {run_status}")

        # Добавляем сообщение пользователя в тред
        assistant_client.add_message_to_thread(thread.thread_id, user_input)

        # Запускаем ассистента
        run = assistant_client.run_assistant(thread, appointment)

        # Ждем завершения запуска
        run_status = assistant_client.wait_for_run_completion(
            thread_id=thread.thread_id,
            run_id=run.run_id
        )

        # Получаем последнее сообщение ассистента
        messages = assistant_client.get_messages(thread.thread_id, limit=1)
        if not messages:
            return JsonResponse({
                'status': 'error',
                'message': 'Не удалось получить ответ от ассистента'
            }, status=500)

        # Извлекаем текст ответа
        assistant_message = messages[0]
        response_text = ""

        # OpenAI может возвращать текст в разных форматах, обрабатываем их
        if assistant_message.content:
            for content_part in assistant_message.content:
                if content_part.type == 'text':
                    response_text += content_part.text.value

        # Проверяем, содержит ли ответ JSON с ожидаемой структурой
        try:
            # Пытаемся найти и извлечь JSON-объект из текста
            import re
            json_match = re.search(r'\{.*\}', response_text, re.DOTALL)

            if json_match:
                json_str = json_match.group(0)
                response_data = json.loads(json_str)

                # Если есть поле status, считаем, что это валидный ответ
                if 'status' in response_data:
                    # Форматируем ответ в соответствии с документацией
                    formatted_response = format_response(response_data['status'], response_data)
                    return JsonResponse(formatted_response)

            # Если не удалось найти валидный JSON, возвращаем ответ ассистента в поле message
            return JsonResponse({
                'status': 'assistant_response',
                'message': response_text.strip()
            })

        except Exception as e:
            logger.error(f"Error parsing assistant response: {e}")
            # В случае ошибки при разборе JSON, возвращаем текст ответа
            return JsonResponse({
                'status': 'assistant_response',
                'message': response_text.strip()
            })

    except json.JSONDecodeError:
        return JsonResponse({
            'status': 'error_bad_input',
            'message': 'Неверный формат JSON'
        }, status=400)
    except Exception as e:
        logger.error(f"Error processing request: {e}", exc_info=True)
        return JsonResponse({
            'status': 'error',
            'message': f'Ошибка обработки запроса: {str(e)}'
        }, status=500)


@csrf_exempt
@require_http_methods(["POST"])
def create_assistant(request):
    """
    Создает или обновляет ассистента в OpenAI и сохраняет его в БД
    """
    try:
        data = json.loads(request.body)
        name = data.get('name', 'Медицинский ассистент')
        instructions = data.get('instructions', '')
        model = data.get('model', 'gpt-4-mini')

        from openai import OpenAI
        from reminder.openai_assistant.assistant_tools import create_assistant_with_tools

        client = OpenAI(api_key=settings.OPEN_AI_API_KEY)

        # Создаем ассистента в OpenAI
        assistant_info = create_assistant_with_tools(
            client=client,
            name=name,
            instructions=instructions,
            model=model
        )

        # Сохраняем или обновляем ассистента в БД
        assistant, created = Assistant.objects.update_or_create(
            assistant_id=assistant_info.id,
            defaults={
                'name': name,
                'model': model,
                'instructions': instructions
            }
        )

        return JsonResponse({
            'status': 'success',
            'message': f"Ассистент {'создан' if created else 'обновлен'} успешно",
            'assistant_id': assistant.assistant_id,
            'name': assistant.name
        })

    except Exception as e:
        logger.error(f"Error creating assistant: {e}", exc_info=True)
        return JsonResponse({
            'status': 'error',
            'message': f'Ошибка создания ассистента: {str(e)}'
        }, status=500)


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
