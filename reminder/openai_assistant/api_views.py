import json
import logging
import re
from datetime import datetime, timezone

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.conf import settings
from django.utils import timezone

from reminder.models import Patient, Appointment, Assistant, Thread, Run, IgnoredPatient
from reminder.openai_assistant.assistant_tools import format_response

logger = logging.getLogger(__name__)

# Import the updated AssistantClient
from reminder.openai_assistant.assistant_client import AssistantClient


@csrf_exempt
@require_http_methods(["POST"])
def process_voicebot_request(request):
    """
    Processes request from the ACS voice robot.

    Expects JSON with fields:
    - appointment_id: Appointment ID
    - user_input: Text input from user

    Returns:
    - JSON with formatted response according to documentation
    """
    try:
        # Parse request data
        data = json.loads(request.body)
        appointment_id = data.get('appointment_id')
        user_input = data.get('user_input')

        logger.info(f"Received request: appointment_id={appointment_id}, user_input='{user_input}'")

        if not appointment_id or not user_input:
            logger.warning("Missing required parameters")
            return JsonResponse({
                'status': 'error_bad_input',
                'message': 'Missing required parameters: appointment_id and user_input'
            }, status=400)

        # Check if appointment exists
        try:
            appointment = Appointment.objects.get(appointment_id=appointment_id)
        except Appointment.DoesNotExist:
            logger.error(f"Appointment {appointment_id} not found")
            return JsonResponse({
                'status': 'error_reception_unavailable',
                'message': 'Appointment not active or not found'
            }, status=404)

        # Check if patient is on ignore list
        if IgnoredPatient.objects.filter(patient_code=appointment.patient.patient_code).exists():
            logger.warning(f"Patient {appointment.patient.patient_code} is on ignore list")
            return JsonResponse({
                'status': 'error_ignored_patient',
                'message': f'Patient with code {appointment.patient.patient_code} is on ignore list.'
            }, status=403)

        # Initialize assistant client
        assistant_client = AssistantClient()

        # Get or create thread for dialogue
        thread = assistant_client.get_or_create_thread(appointment_id)

        # If there's a running process that's not complete, wait for it to finish
        if thread.current_run and thread.current_run.status not in ['completed', 'failed', 'cancelled', 'expired']:
            logger.info(f"Waiting for previous run {thread.current_run.run_id} to complete")
            run_status = assistant_client.wait_for_run_completion(
                thread_id=thread.thread_id,
                run_id=thread.current_run.run_id
            )
            logger.info(f"Previous run completed with status: {run_status}")

        # Add user message to thread
        assistant_client.add_message_to_thread(thread.thread_id, user_input)

        # Run assistant
        logger.info(f"Starting new assistant run for thread {thread.thread_id}")
        run = assistant_client.run_assistant(thread, appointment)

        # Wait for run to complete
        run_status = assistant_client.wait_for_run_completion(
            thread_id=thread.thread_id,
            run_id=run.run_id,
            timeout=60  # 60 seconds timeout
        )
        logger.info(f"Assistant run completed with status: {run_status}")

        # Get latest assistant message
        messages = assistant_client.get_messages(thread.thread_id, limit=1)
        if not messages:
            logger.error("No response received from assistant")
            return JsonResponse({
                'status': 'error',
                'message': 'Failed to get response from assistant'
            }, status=500)

        # Extract response text
        assistant_message = messages[0]
        response_text = ""

        # OpenAI may return content in different formats
        if assistant_message.content:
            for content_part in assistant_message.content:
                if content_part.type == 'text':
                    response_text += content_part.text.value

        logger.info(f"Raw assistant response: {response_text[:500]}...")  # Log first 500 chars

        # Try to find and extract JSON object from text
        try:
            # Use regex to find JSON object in text
            json_match = re.search(r'\{.*\}', response_text, re.DOTALL)

            if json_match:
                json_str = json_match.group(0)
                response_data = json.loads(json_str)

                # If response has status field, it's a valid response
                if 'status' in response_data:
                    # Format response according to documentation
                    formatted_response = format_response(response_data['status'], response_data)
                    logger.info(f"Returning formatted response: {formatted_response}")
                    return JsonResponse(formatted_response)

            # Special handling for requests about available time slots
            if "свободные окошки" in user_input.lower() or "когда можно записаться" in user_input.lower():
                # If the assistant didn't call the correct function, we'll do it directly
                logger.warning("Assistant failed to call which_time_in_certain_day function, calling it directly")

                from reminder.infoclinica_requests.schedule.which_time_in_certain_day import which_time_in_certain_day

                # Determine if the query is for today
                today = datetime.now().strftime("%Y-%m-%d")
                response = which_time_in_certain_day(appointment.patient.patient_code, today)

                # Convert JsonResponse to dict if needed
                if hasattr(response, 'content'):
                    return response
                else:
                    return JsonResponse(response)

            # If no JSON and no special handling, return the text response
            logger.info("No valid JSON found in response, returning text")
            return JsonResponse({
                'status': 'assistant_response',
                'message': response_text.strip()
            })

        except Exception as e:
            logger.error(f"Error parsing assistant response: {e}", exc_info=True)
            # In case of error parsing JSON, return text response
            return JsonResponse({
                'status': 'assistant_response',
                'message': response_text.strip()
            })

    except json.JSONDecodeError:
        logger.error("Invalid JSON format in request")
        return JsonResponse({
            'status': 'error_bad_input',
            'message': 'Invalid JSON format'
        }, status=400)
    except Exception as e:
        logger.error(f"Error processing request: {e}", exc_info=True)
        return JsonResponse({
            'status': 'error',
            'message': f'Error processing request: {str(e)}'
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
