import os
import re
import json
import logging
import time as time_module
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Union

from django.http.response import JsonResponse
from django.utils import timezone
from django.conf import settings
from openai import OpenAI
from django.db import transaction

from reminder.infoclinica_requests.schedule.appointment_time_for_patient import appointment_time_for_patient
from reminder.infoclinica_requests.schedule.delete_reception_for_patient import delete_reception_for_patient
from reminder.infoclinica_requests.schedule.reserve_reception_for_patient import reserve_reception_for_patient
from reminder.infoclinica_requests.schedule.which_time_in_certain_day import which_time_in_certain_day
from reminder.models import Assistant, Thread, Run as RunModel, Patient, Appointment, QueueInfo, AvailableTimeSlot
from reminder.openai_assistant.assistant_instructions import get_enhanced_assistant_prompt

logger = logging.getLogger(__name__)

# Mapping of times of day to specific hours
TIME_MAPPINGS = {
    # Morning
    "утро": "10:30",
    "утром": "10:30",
    "с утра": "10:30",
    "на утро": "10:30",
    "пораньше": "10:30",
    "рано": "10:30",
    "раннее": "10:30",

    # Noon
    "обед": "13:30",
    "на обед": "13:30",
    "в обед": "13:30",
    "полдень": "13:30",
    "в полдень": "13:30",
    "дневное": "13:30",
    "днем": "13:30",

    # Evening
    "вечер": "18:30",
    "вечером": "18:30",
    "на вечер": "18:30",
    "ужин": "18:30",
    "на ужин": "18:30",
    "к ужину": "18:30",
    "поздно": "18:30",
    "попозже": "18:30",
    "позднее": "18:30"
}

# Constants for date formatting
MONTHS_RU = {
    1: "Января", 2: "Февраля", 3: "Марта", 4: "Апреля", 5: "Мая", 6: "Июня",
    7: "Июля", 8: "Августа", 9: "Сентября", 10: "Октября", 11: "Ноября", 12: "Декабря"
}

MONTHS_KZ = {
    1: "Қаңтар", 2: "Ақпан", 3: "Наурыз", 4: "Сәуір", 5: "Мамыр", 6: "Маусым",
    7: "Шілде", 8: "Тамыз", 9: "Қыркүйек", 10: "Қазан", 11: "Қараша", 12: "Желтоқсан"
}

WEEKDAYS_RU = {
    0: "Понедельник", 1: "Вторник", 2: "Среда", 3: "Четверг", 4: "Пятница", 5: "Суббота", 6: "Воскресенье"
}

WEEKDAYS_KZ = {
    0: "Дүйсенбі", 1: "Сейсенбі", 2: "Сәрсенбі", 3: "Бейсенбі", 4: "Жұма", 5: "Сенбі", 6: "Жексенбі"
}


class AssistantClient:
    """
    Improved client for working with OpenAI Assistant API.
    Provides interface for patient interactions with appointment system.
    """

    def __init__(self):
        """Initialize client with API key from settings."""
        self.client = OpenAI(api_key=settings.OPENAI_API_KEY)

    def get_or_create_thread(self, thread_identifier, entity):
        """
        Improved function that ensures consistent thread usage for the same patient.
        """
        try:
            # Look for an active thread with this identifier
            existing_thread = Thread.objects.filter(
                order_key=str(thread_identifier),
                expires_at__gt=timezone.now()
            ).first()

            if existing_thread:
                logger.info(f"Найден существующий тред {existing_thread.thread_id} для {thread_identifier}")

                # Check for active runs that could block new messages
                try:
                    runs = self.client.beta.threads.runs.list(
                        thread_id=existing_thread.thread_id,
                        limit=1
                    )

                    if runs.data and runs.data[0].status in ["in_progress", "queued", "requires_action"]:
                        logger.warning(f"Тред {existing_thread.thread_id} имеет активный запуск, отменяем его")
                        try:
                            self.client.beta.threads.runs.cancel(
                                thread_id=existing_thread.thread_id,
                                run_id=runs.data[0].id
                            )
                        except Exception as cancel_error:
                            logger.error(f"Ошибка при отмене запуска: {cancel_error}")
                            # Even if cancellation fails, continue with the existing thread
                except Exception as run_check_error:
                    logger.error(f"Ошибка при проверке запусков: {run_check_error}")

                # Update thread expiration
                existing_thread.expires_at = timezone.now() + timedelta(hours=24)
                existing_thread.save()

                return existing_thread

            # If no thread found, create a new one
            return self._create_fresh_thread(entity)

        except Exception as e:
            logger.error(f"Ошибка в get_or_create_thread: {e}", exc_info=True)
            # If an error occurs, create a new thread
            return self._create_fresh_thread(entity)

    def _create_fresh_thread(self, entity):
        """
        Creates a completely new thread for an appointment or patient.
        Enhanced to work with both Appointment and Patient objects.

        Args:
            entity: Either Appointment object or Patient object

        Returns:
            Thread: New thread object
        """
        try:
            # Get first assistant from DB
            assistant = Assistant.objects.first()
            if not assistant:
                logger.error("No assistants found in database")
                raise ValueError("No assistants found in database")

            # Create a new thread in OpenAI
            openai_thread = self.client.beta.threads.create()

            # Determine if we're working with an appointment or directly with a patient
            is_appointment = isinstance(entity, Appointment)

            if is_appointment:
                appointment = entity
                thread_order_key = str(appointment.appointment_id)
                appointment_id = appointment.appointment_id
            else:
                patient = entity
                thread_order_key = f"patient_{patient.patient_code}"
                appointment_id = None

            # Save thread in local database
            thread = Thread.objects.create(
                thread_id=openai_thread.id,
                order_key=thread_order_key,
                assistant=assistant,
                appointment_id=appointment_id
            )

            if is_appointment:
                logger.info(f"Created new thread {thread.thread_id} for appointment {appointment.appointment_id}")
            else:
                logger.info(f"Created new thread {thread.thread_id} for patient {patient.patient_code}")

            return thread

        except Exception as e:
            logger.error(f"Error creating new thread: {e}", exc_info=True)
            raise

    def add_message_to_thread(self, thread_id: str, content: str, role: str = "user") -> dict:
        """
        Adds a message to a thread with enhanced error handling.

        Args:
            thread_id: Thread ID
            content: Message content
            role: Message role (usually "user")

        Returns:
            dict: Information about the created message
        """
        try:
            message = self.client.beta.threads.messages.create(
                thread_id=thread_id,
                role=role,
                content=content
            )
            logger.info(f"Added message from {role} to thread {thread_id}")
            return message
        except Exception as e:
            error_message = str(e)

            # Check if the error is about an active run
            if "Can't add messages to thread" in error_message and "while a run" in error_message:
                logger.warning(f"Thread {thread_id} has an active run, cannot add message. Getting run details...")

                # Find and cancel the active run
                try:
                    runs = self.client.beta.threads.runs.list(
                        thread_id=thread_id,
                        limit=1
                    )

                    if runs.data and runs.data[0].status in ["in_progress", "queued", "requires_action"]:
                        run_id = runs.data[0].id
                        logger.warning(f"Found active run {run_id}. Attempting to cancel...")

                        try:
                            self.client.beta.threads.runs.cancel(
                                thread_id=thread_id,
                                run_id=run_id
                            )
                            logger.info(f"Successfully cancelled run {run_id}")

                            # Wait a moment for the cancellation to take effect
                            time_module.sleep(2)

                            # Try adding message again
                            message = self.client.beta.threads.messages.create(
                                thread_id=thread_id,
                                role=role,
                                content=content
                            )
                            logger.info(f"Successfully added message after cancelling run")
                            return message
                        except Exception as cancel_e:
                            logger.error(f"Error cancelling run: {cancel_e}")
                            raise ValueError("Cannot add message - active run could not be cancelled")
                except Exception as list_e:
                    logger.error(f"Error listing runs: {list_e}")
                    raise ValueError("Cannot add message - unable to list active runs")

            # For other errors
            logger.error(f"Error adding message to thread: {e}")
            raise

    def run_assistant(self, thread, entity, instructions=None):
        """
        Запускает ассистента с улучшенной обработкой контекста.
        """
        try:
            # Get thread ID
            thread_id = thread.thread_id

            # Get the last few messages to extract context
            messages = self.client.beta.threads.messages.list(
                thread_id=thread_id,
                limit=10,  # Increased limit to ensure we catch context
                order="desc"
            )

            # Extract context from previous assistant messages
            last_times_message = None
            date_from_context = None
            day_from_context = None
            time_slots = []

            for message in messages.data:
                if message.role == "assistant" and hasattr(message, 'content') and message.content:
                    content_text = ""
                    for content_item in message.content:
                        if hasattr(content_item, 'text') and content_item.text:
                            content_text += content_item.text.value

                    # Check for time-related statuses in JSON format
                    if any(marker in content_text for marker in
                           ["first_time", "second_time", "third_time", "which_time"]):
                        last_times_message = content_text

                        # Extract date information
                        date_pattern = r'"date":\s*"([^"]+)"'
                        date_match = re.search(date_pattern, content_text)

                        # Extract day information (today/tomorrow)
                        day_pattern = r'"day":\s*"([^"]+)"'
                        day_match = re.search(day_pattern, content_text)

                        # Extract time slots
                        time_pattern = r'"(first_time|second_time|third_time)":\s*"([^"]+)"'
                        time_matches = re.findall(time_pattern, content_text)

                        if time_matches:
                            time_slots = [time_value for _, time_value in time_matches]

                        if date_match:
                            date_from_context = date_match.group(1)

                        if day_match:
                            day_from_context = day_match.group(1)

                        break

            # Build powerful context instructions if we found relevant previous context
            context_instructions = ""
            if last_times_message and (date_from_context or day_from_context) and time_slots:
                context_instructions = f"""
                ## КРИТИЧЕСКИ ВАЖНЫЙ ПРЕДЫДУЩИЙ КОНТЕКСТ

                В предыдущем ответе я показал пациенту следующие доступные времена:

                ### Дата: {date_from_context or "Неизвестно"}"""

                if day_from_context:
                    context_instructions += f" ({day_from_context})"

                context_instructions += "\n\n"

                # Add time slots with explicit labeling
                for i, time_value in enumerate(time_slots):
                    slot_label = {0: "Первое время", 1: "Второе время", 2: "Третье время"}.get(i, f"Время {i + 1}")
                    context_instructions += f"- {slot_label}: {time_value}\n"

                context_instructions += """
                ## ПРАВИЛА ОБРАБОТКИ ССЫЛОК НА ПРЕДЫДУЩИЕ ВРЕМЕНА

                КРИТИЧЕСКИ ВАЖНО: Когда пользователь ссылается на времена из предыдущего контекста:

                1. Если пользователь говорит "первое время", "первый вариант", "первое", "на первое окошко" → ему нужно первое время из списка выше
                2. Если пользователь говорит "второе время", "второй вариант", "второе", "на второе окошко" → ему нужно второе время из списка выше
                3. Если пользователь говорит "третье время", "третий вариант", "третье", "на третье окошко" → ему нужно третье время из списка выше
                4. Если пользователь говорит "давай", "ок", "да", "хорошо", "записывай" без уточнения → ему нужно первое время из списка

                КРИТИЧЕСКИ ВАЖНО: Используй именно ту ДАТУ, которая была показана ранее! 
                Не используй сегодняшнюю дату, если в предыдущем ответе была показана дата другого дня!

                Если пациент говорит что-то вроде "а давай на первое окошко" после того, как ему были показаны
                времена на другую дату, запись ДОЛЖНА быть сделана на ПЕРВОЕ ИЗ ПОКАЗАННЫХ ВРЕМЕН с ДАТОЙ ИЗ КОНТЕКСТА,
                а НЕ с сегодняшней датой!
                """

            # Create final instructions by combining context with other instructions
            final_instructions = ""

            if context_instructions:
                final_instructions += context_instructions + "\n\n"

            if instructions:
                final_instructions += instructions
            else:
                # Default instructions if none provided
                final_instructions += """
                # КРИТИЧЕСКИ ВАЖНО: ВСЕГДА ИСПОЛЬЗУЙ ФУНКЦИИ, А НЕ ТЕКСТОВЫЕ ОТВЕТЫ!

                В следующих ситуациях ты ОБЯЗАТЕЛЬНО должен вызвать функцию вместо текстового ответа:

                1. Когда пользователь спрашивает о свободных окошках или времени:
                   → ВСЕГДА вызывай функцию which_time_in_certain_day

                2. Когда пользователь интересуется своей текущей записью:
                   → ВСЕГДА вызывай функцию appointment_time_for_patient

                3. Когда пользователь хочет записаться или перенести запись:
                   → ВСЕГДА вызывай функцию reserve_reception_for_patient

                4. Когда пользователь хочет отменить запись:
                   → ВСЕГДА вызывай функцию delete_reception_for_patient

                # ЗАПРЕЩЕНО использовать текстовые ответы для вышеперечисленных запросов!
                # ВСЕГДА вызывай соответствующую функцию!
                """

            # Create run with final instructions
            try:
                openai_run = self.client.beta.threads.runs.create(
                    thread_id=thread_id,
                    assistant_id=thread.assistant.assistant_id,
                    instructions=final_instructions
                )

                # Save run in database
                run = RunModel.objects.create(
                    run_id=openai_run.id,
                    status=openai_run.status
                )

                # Update thread with current run
                thread.current_run = run
                thread.save()

                logger.info(f"Запущен run {run.run_id} для треда {thread_id} с инструкциями для сохранения контекста")
                return run
            except Exception as run_error:
                logger.error(f"Ошибка при создании запуска: {run_error}", exc_info=True)
                raise

        except Exception as e:
            logger.error(f"Ошибка запуска ассистента: {e}", exc_info=True)
            raise

    def wait_for_run_completion(self, thread_id: str, run_id: str, timeout: int = 30) -> dict:
        """
        Waits for completion of an assistant run with enhanced function call handling.
        Immediately returns formatted function results when available.

        Args:
            thread_id: Thread ID
            run_id: Run ID
            timeout: Maximum wait time in seconds

        Returns:
            dict: Formatted function result or status information
        """
        start_time = time_module.time()
        function_result = None

        # List of valid status codes
        valid_statuses = [
            "success_change_reception", "success_change_reception_today", "success_change_reception_tomorrow",
            "error_change_reception", "error_change_reception_today", "error_change_reception_tomorrow",
            "which_time", "which_time_today", "which_time_tomorrow",
            "error_empty_windows", "error_empty_windows_today", "error_empty_windows_tomorrow",
            "nonworktime", "error_med_element", "no_action_required",
            "success_deleting_reception", "error_deleting_reception", "error_change_reception_bad_date",
            "only_first_time_tomorrow", "only_first_time_today", "only_first_time",
            "only_two_time_tomorrow", "only_two_time_today", "only_two_time",
            "change_only_first_time_tomorrow", "change_only_first_time_today", "change_only_first_time",
            "change_only_two_time_tomorrow", "change_only_two_time_today", "change_only_two_time"
        ]

        try:
            while time_module.time() - start_time < timeout:
                try:
                    run = self.client.beta.threads.runs.retrieve(
                        thread_id=thread_id,
                        run_id=run_id
                    )
                except Exception as retrieve_error:
                    logger.error(f"Error retrieving run {run_id}: {retrieve_error}")
                    return {"status": "bad_user_input"}

                # Update status in database
                self._update_run_status(run_id, run.status)

                # Process function calls if needed
                if run.status == "requires_action" and run.required_action:
                    logger.info(f"Run {run_id} requires action - processing function calls")
                    try:
                        function_result = self._process_function_calls(thread_id, run_id, run)
                    except Exception as func_error:
                        logger.error(f"Error processing function calls: {func_error}")
                        return {"status": "bad_user_input"}

                    # IMPORTANT: Return function result immediately
                    if function_result and isinstance(function_result, dict) and "status" in function_result:
                        # Check if the status is valid
                        if function_result["status"] not in valid_statuses and function_result[
                            "status"] != "bad_user_input":
                            logger.warning(
                                f"Invalid status returned: {function_result['status']}, changing to bad_user_input")
                            return {"status": "bad_user_input"}

                        logger.info(f"Returning immediate function result with status: {function_result['status']}")
                        return function_result

                if run.status in ["completed", "failed", "cancelled", "expired"]:
                    logger.info(f"Run {run_id} ended with status: {run.status}")

                    # If run failed, cancelled, or expired, return bad_user_input
                    if run.status in ["failed", "cancelled", "expired"]:
                        logger.warning(f"Run {run_id} ended with status {run.status}, returning bad_user_input")
                        return {"status": "bad_user_input"}

                    # If we have a function result, validate and return it
                    if function_result:
                        # Check if the status is valid
                        if function_result.get("status") not in valid_statuses and function_result.get(
                                "status") != "bad_user_input":
                            logger.warning(
                                f"Invalid status in function result: {function_result.get('status')}, changing to bad_user_input")
                            return {"status": "bad_user_input"}
                        return function_result

                    # Check if messages contain any function references we can process
                    try:
                        messages = self.get_messages(thread_id, limit=1)
                        if messages and hasattr(messages[0], 'content') and messages[0].content:
                            for content_item in messages[0].content:
                                if hasattr(content_item, 'text') and content_item.text:
                                    text = content_item.text.value
                                    # Look for function call patterns in the text
                                    function_result = self._extract_function_calls_from_text(text, thread_id)
                                    if function_result:
                                        # Validate the status
                                        if function_result.get("status") not in valid_statuses and function_result.get(
                                                "status") != "bad_user_input":
                                            logger.warning(
                                                f"Invalid status in extracted function: {function_result.get('status')}, changing to bad_user_input")
                                            return {"status": "bad_user_input"}
                                        return function_result
                    except Exception as msg_error:
                        logger.error(f"Error processing messages: {msg_error}")
                        return {"status": "bad_user_input"}

                    # Before returning just the status, check if the query looks like a request for available times
                    try:
                        messages = self.get_messages(thread_id, limit=5)
                        for message in messages:
                            if message.role == "user":
                                if message.content and len(message.content) > 0 and hasattr(message.content[0], 'text'):
                                    text = message.content[0].text.value.lower()
                                    if any(word in text for word in
                                           ["свободн", "доступн", "времена", "окошк", "запис"]):
                                        # Try to extract date from request
                                        date_str = "today"
                                        if "завтра" in text:
                                            date_str = "tomorrow"
                                        elif "послезавтра" in text or "после завтра" in text:
                                            date_str = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")

                                        # Force call to which_time_in_certain_day
                                        patient_code = None
                                        thread = Thread.objects.filter(thread_id=thread_id).first()
                                        if thread and thread.appointment_id:
                                            appointment = Appointment.objects.filter(
                                                appointment_id=thread.appointment_id).first()
                                            if appointment:
                                                patient_code = appointment.patient.patient_code
                                        else:
                                            # Try to get patient code from thread order_key
                                            thread_obj = Thread.objects.filter(thread_id=thread_id).first()
                                            if thread_obj and thread_obj.order_key and thread_obj.order_key.startswith(
                                                    "patient_"):
                                                patient_code = thread_obj.order_key.replace("patient_", "")

                                        if patient_code:
                                            try:
                                                result = self._call_function("which_time_in_certain_day",
                                                                             {"patient_code": patient_code,
                                                                              "date_time": date_str}, thread_id)
                                                formatted_result = self._format_for_acs("which_time_in_certain_day",
                                                                                        {"patient_code": patient_code,
                                                                                         "date_time": date_str}, result)
                                                if formatted_result and "status" in formatted_result:
                                                    # Validate the status
                                                    if formatted_result["status"] not in valid_statuses and \
                                                            formatted_result["status"] != "bad_user_input":
                                                        logger.warning(
                                                            f"Invalid status in formatted result: {formatted_result['status']}, changing to bad_user_input")
                                                        return {"status": "bad_user_input"}
                                                    return formatted_result
                                            except Exception as func_error:
                                                logger.error(f"Error calling function: {func_error}")
                                                return {"status": "bad_user_input"}
                    except Exception as analyze_error:
                        logger.error(f"Error analyzing user messages: {analyze_error}")
                        return {"status": "bad_user_input"}

                    # Last resort: if completed but no valid result, return bad_user_input
                    return {"status": "bad_user_input"}

                # Wait before checking again
                time_module.sleep(1)

            # If we reach here, timeout exceeded
            logger.warning(f"Run {run_id} exceeded timeout {timeout}s, cancelling")
            self._cancel_run(thread_id, run_id)
            return {"status": "bad_user_input"}

        except Exception as e:
            logger.error(f"Error in wait_for_run_completion: {e}", exc_info=True)
            return {"status": "bad_user_input"}

    def _process_function_calls(self, thread_id: str, run_id: str, run) -> dict:
        """
        Processes function calls from the assistant and formats responses.

        Args:
            thread_id: Thread ID
            run_id: Run ID
            run: Run object with function calls

        Returns:
            dict: Formatted function result or None
        """
        try:
            tool_calls = run.required_action.submit_tool_outputs.tool_calls
            logger.info(f"Processing {len(tool_calls)} function calls")

            # Process each function call
            tool_outputs = []
            last_formatted_result = None

            for tool_call in tool_calls:
                function_name = tool_call.function.name
                function_args = json.loads(tool_call.function.arguments)

                logger.info(f"Processing function call: {function_name} with args: {function_args}")

                # Call the function
                raw_result = self._call_function(function_name, function_args, thread_id)

                # Format result for ACS
                formatted_result = self._format_for_acs(function_name, function_args, raw_result)
                logger.info(f"Formatted ACS result: {formatted_result}")

                # Add to outputs
                tool_outputs.append({
                    "tool_call_id": tool_call.id,
                    "output": json.dumps(raw_result)
                })

                # Store the last valid formatted result
                if formatted_result and "status" in formatted_result:
                    last_formatted_result = formatted_result

            # Submit all tool outputs in one go
            try:
                self.client.beta.threads.runs.submit_tool_outputs(
                    thread_id=thread_id,
                    run_id=run_id,
                    tool_outputs=tool_outputs
                )
                logger.info(f"Submitted {len(tool_outputs)} tool outputs")
            except Exception as submit_e:
                logger.error(f"Error submitting tool outputs: {submit_e}")
                # If submission fails, try to submit outputs for each call individually
                try:
                    for output in tool_outputs:
                        self.client.beta.threads.runs.submit_tool_outputs(
                            thread_id=thread_id,
                            run_id=run_id,
                            tool_outputs=[output]
                        )
                    logger.info("Successfully submitted tool outputs individually")
                except Exception as individual_submit_e:
                    logger.error(f"Error submitting tool outputs individually: {individual_submit_e}")

            # Return the last valid formatted result
            return last_formatted_result

        except Exception as e:
            logger.error(f"Error processing function calls: {e}", exc_info=True)
            return {"status": "error_med_element", "message": str(e)}

    def _extract_function_calls_from_text(self, text: str, thread_id: str) -> Optional[dict]:
        """
        Extracts and processes function calls mentioned in text responses.

        Args:
            text: Text containing function call references
            thread_id: Thread ID for context

        Returns:
            Optional[dict]: Formatted function result or None
        """
        # Look for function calls in the format: function_name(param1=value1, param2=value2)
        function_pattern = r'([a-zA-Z_]+)\((.*?)\)'
        matches = re.findall(function_pattern, text)

        if not matches:
            return None

        try:
            for function_name, args_str in matches:
                # Parse arguments
                args_dict = {}

                # Split by commas not inside quotes
                parts = re.findall(r'([^,]+?(?:,(?=\s*[\"\'])|$)|\".+?\"|\'.+?\')', args_str)

                for part in parts:
                    part = part.strip()
                    if '=' in part:
                        key, value = part.split('=', 1)
                        key = key.strip()
                        value = value.strip().strip('\'"')  # Remove quotes
                        args_dict[key] = value

                # Skip if not a recognized function
                if function_name not in [
                    "which_time_in_certain_day",
                    "appointment_time_for_patient",
                    "reserve_reception_for_patient",
                    "delete_reception_for_patient"
                ]:
                    continue

                # Call the function
                raw_result = self._call_function(function_name, args_dict, thread_id)

                # Format result for ACS
                formatted_result = self._format_for_acs(function_name, args_dict, raw_result)

                # Return first valid result found
                if formatted_result and "status" in formatted_result:
                    return formatted_result

            return None
        except Exception as e:
            logger.error(f"Error extracting function calls from text: {e}", exc_info=True)
            return None

    def _call_function(self, function_name: str, function_args: dict, thread_id: str = None) -> dict:
        """
        Calls the appropriate function with given arguments.

        Args:
            function_name: Name of function to call
            function_args: Arguments for the function
            thread_id: Optional thread ID for context

        Returns:
            dict: Function result
        """
        # Import necessary functions
        from reminder.infoclinica_requests.schedule.delete_reception_for_patient import delete_reception_for_patient
        from reminder.infoclinica_requests.schedule.reserve_reception_for_patient import reserve_reception_for_patient
        from reminder.infoclinica_requests.schedule.appointment_time_for_patient import appointment_time_for_patient
        from reminder.infoclinica_requests.schedule.which_time_in_certain_day import which_time_in_certain_day

        try:
            logger.info(f"Calling function {function_name} with args: {function_args}")

            # Function mapping and execution
            if function_name == "which_time_in_certain_day":
                patient_code = function_args.get("patient_code")
                date_time = function_args.get("date_time")

                # Handle special date values
                if date_time == "today":
                    date_time = datetime.now().strftime("%Y-%m-%d")
                elif date_time == "tomorrow":
                    date_time = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

                # Call function
                result = which_time_in_certain_day(patient_code, date_time)

                # Convert JsonResponse to dict if needed
                if hasattr(result, 'content'):
                    return json.loads(result.content.decode('utf-8'))
                return result

            elif function_name == "appointment_time_for_patient":
                patient_code = function_args.get("patient_code")
                year_from_patient = function_args.get("year_from_patient_for_returning")

                # Call function
                result = appointment_time_for_patient(patient_code, year_from_patient)

                # Convert JsonResponse to dict if needed
                if hasattr(result, 'content'):
                    return json.loads(result.content.decode('utf-8'))
                return result

            elif function_name == "reserve_reception_for_patient":

                # Проверяем оба возможных имени параметра

                patient_id = function_args.get("patient_id") or function_args.get("id")
                date_from_patient = function_args.get("date_from_patient")
                trigger_id = function_args.get("trigger_id", 1)  # По умолчанию 1

                # Если нет конкретного времени, добавляем 10:30
                if date_from_patient and ' ' not in date_from_patient:
                    date_from_patient = f"{date_from_patient} 10:30"

                result = reserve_reception_for_patient(patient_id, date_from_patient, trigger_id)

                # Process date_from_patient
                if date_from_patient:
                    # Apply time of day mappings if needed
                    user_message = self._get_last_user_message(thread_id) if thread_id else ""
                    user_message = user_message.lower()

                    # If date_from_patient only has date part, add time
                    if ' ' not in date_from_patient:
                        # Default time is 10:30 (morning)
                        time_str = "10:30"

                        # Map to specific times based on time of day mentions in user message
                        for period, specific_time in TIME_MAPPINGS.items():
                            if period in user_message:
                                time_str = specific_time
                                break

                        date_from_patient = f"{date_from_patient} {time_str}"

                # Call function
                result = reserve_reception_for_patient(patient_id, date_from_patient, trigger_id)

                # Convert JsonResponse to dict if needed
                if hasattr(result, 'content'):
                    return json.loads(result.content.decode('utf-8'))
                return result

            elif function_name == "delete_reception_for_patient":
                patient_id = function_args.get("patient_id")

                # Call function
                result = delete_reception_for_patient(patient_id)

                # Convert JsonResponse to dict if needed
                if hasattr(result, 'content'):
                    return json.loads(result.content.decode('utf-8'))
                return result

            else:
                logger.warning(f"Unknown function: {function_name}")
                return {"status": "error", "message": f"Unknown function: {function_name}"}

        except Exception as e:
            logger.error(f"Error calling function {function_name}: {e}", exc_info=True)
            return {"status": "error", "message": str(e)}

    def _get_last_user_message(self, thread_id: str) -> str:
        """
        Gets the last user message from a thread.

        Args:
            thread_id: Thread ID

        Returns:
            str: Last user message content
        """
        try:
            messages = self.client.beta.threads.messages.list(
                thread_id=thread_id,
                limit=5,
                order="desc"
            )

            for message in messages.data:
                if message.role == "user":
                    if message.content and len(message.content) > 0 and hasattr(message.content[0], 'text'):
                        return message.content[0].text.value

            return ""
        except Exception as e:
            logger.error(f"Error getting last user message: {e}")
            return ""

    def _format_for_acs(self, function_name: str, function_args: dict, result: dict) -> dict:
        """
        Formats function results for ACS according to required status formats.

        Args:
            function_name: Name of the called function
            function_args: Arguments passed to the function
            result: Raw function result

        Returns:
            dict: Formatted response for ACS
        """
        try:
            # Extract date information if available
            date_obj = None
            date_str = None

            # Look for date in various fields
            date_fields = ["date", "date_time", "date_from_patient", "appointment_date"]
            for field in date_fields:
                if field in function_args and function_args[field]:
                    date_str = function_args[field]
                    break

            # Parse date string
            if date_str:
                try:
                    if date_str == "today":
                        date_obj = datetime.now()
                    elif date_str == "tomorrow":
                        date_obj = datetime.now() + timedelta(days=1)
                    elif " " in date_str:  # Date with time (YYYY-MM-DD HH:MM)
                        date_obj = datetime.strptime(date_str.split(" ")[0], "%Y-%m-%d")
                    else:  # Date only (YYYY-MM-DD)
                        date_obj = datetime.strptime(date_str, "%Y-%m-%d")
                except ValueError:
                    pass

            # Determine relation to today/tomorrow
            relation = None
            if date_obj:
                today = datetime.now().date()
                tomorrow = today + timedelta(days=1)
                if date_obj.date() == today:
                    relation = "today"
                elif date_obj.date() == tomorrow:
                    relation = "tomorrow"

            # Format based on function type
            if function_name == "which_time_in_certain_day":
                # Extract available times
                available_times = []
                if "all_available_times" in result and isinstance(result["all_available_times"], list):
                    available_times = result["all_available_times"]
                elif "suggested_times" in result and isinstance(result["suggested_times"], list):
                    available_times = result["suggested_times"]
                else:
                    # Check standard time fields
                    for key in ["first_time", "second_time", "third_time"]:
                        if key in result and result[key]:
                            available_times.append(result[key])

                    # Check numeric time fields
                    for i in range(1, 10):
                        key = f"time_{i}"
                        if key in result and result[key]:
                            available_times.append(result[key])

                # Clean times (remove date part, seconds)
                clean_times = []
                for t in available_times:
                    if isinstance(t, str):
                        if " " in t:  # Format: "YYYY-MM-DD HH:MM"
                            clean_times.append(t.split(" ")[1])
                        else:
                            # Remove seconds if present
                            if t.count(":") == 2:  # Format: "HH:MM:SS"
                                clean_times.append(":".join(t.split(":")[:2]))
                            else:
                                clean_times.append(t)

                # Format date information if available
                date_info = {}
                if date_obj:
                    day = date_obj.day
                    month_num = date_obj.month
                    weekday_idx = date_obj.weekday()

                    date_info = {
                        "date": f"{day} {MONTHS_RU[month_num]}",
                        "date_kz": f"{day} {MONTHS_KZ[month_num]}",
                        "weekday": WEEKDAYS_RU[weekday_idx],
                        "weekday_kz": WEEKDAYS_KZ[weekday_idx]
                    }

                # No available times
                if not clean_times:
                    status = "error_empty_windows"
                    if relation == "today":
                        status = "error_empty_windows_today"
                    elif relation == "tomorrow":
                        status = "error_empty_windows_tomorrow"

                    response = {
                        "status": status,
                        "message": "Свободных приемов не найдено."
                    }

                    if relation == "today":
                        response["day"] = "сегодня"
                        response["day_kz"] = "бүгін"
                    elif relation == "tomorrow":
                        response["day"] = "завтра"
                        response["day_kz"] = "ертең"

                    return response

                # One available time
                elif len(clean_times) == 1:
                    status = "only_first_time"
                    if relation == "today":
                        status = "only_first_time_today"
                    elif relation == "tomorrow":
                        status = "only_first_time_tomorrow"

                    response = {
                        "status": status,
                        **date_info,
                        "specialist_name": result.get("specialist_name", "Специалист"),
                        "first_time": clean_times[0]
                    }

                    if relation == "today":
                        response["day"] = "сегодня"
                        response["day_kz"] = "бүгін"
                    elif relation == "tomorrow":
                        response["day"] = "завтра"
                        response["day_kz"] = "ертең"

                    return response

                # Two available times
                elif len(clean_times) == 2:
                    status = "only_two_time"
                    if relation == "today":
                        status = "only_two_time_today"
                    elif relation == "tomorrow":
                        status = "only_two_time_tomorrow"

                    response = {
                        "status": status,
                        **date_info,
                        "specialist_name": result.get("specialist_name", "Специалист"),
                        "first_time": clean_times[0],
                        "second_time": clean_times[1]
                    }

                    if relation == "today":
                        response["day"] = "сегодня"
                        response["day_kz"] = "бүгін"
                    elif relation == "tomorrow":
                        response["day"] = "завтра"
                        response["day_kz"] = "ертең"

                    return response

                # Three or more available times
                else:
                    status = "which_time"
                    if relation == "today":
                        status = "which_time_today"
                    elif relation == "tomorrow":
                        status = "which_time_tomorrow"

                    response = {
                        "status": status,
                        **date_info,
                        "specialist_name": result.get("specialist_name", "Специалист"),
                        "first_time": clean_times[0],
                        "second_time": clean_times[1],
                        "third_time": clean_times[2]
                    }

                    if relation == "today":
                        response["day"] = "сегодня"
                        response["day_kz"] = "бүгін"
                    elif relation == "tomorrow":
                        response["day"] = "завтра"
                        response["day_kz"] = "ертең"

                    return response

            elif function_name == "reserve_reception_for_patient":
                # Extract date information
                date_info = {}
                if date_obj:
                    day = date_obj.day
                    month_num = date_obj.month
                    weekday_idx = date_obj.weekday()

                    date_info = {
                        "date": f"{day} {MONTHS_RU[month_num]}",
                        "date_kz": f"{day} {MONTHS_KZ[month_num]}",
                        "weekday": WEEKDAYS_RU[weekday_idx],
                        "weekday_kz": WEEKDAYS_KZ[weekday_idx]
                    }

                # Исправление в методе _format_for_acs в классе AssistantClient
                # Найдите этот блок кода в функции _format_for_acs:

                # Success case
                if result.get("status") in ["success", "success_schedule", "success_change_reception"] or \
                        result.get("status", "").startswith("success_change_reception"):

                    # Get time from response
                    time = result.get("time", "")
                    if " " in time:  # Format: "YYYY-MM-DD HH:MM"
                        time = time.split(" ")[1]
                    if time.count(":") == 2:  # Format: "HH:MM:SS"
                        time = ":".join(time.split(":")[:2])

                    # Определение правильного статуса в зависимости от даты
                    status = "success_change_reception"
                    if relation == "today":
                        status = "success_change_reception_today"
                    elif relation == "tomorrow":
                        status = "success_change_reception_tomorrow"

                    response = {
                        "status": status,  # Использовать правильный статус из списка допустимых
                        **date_info,
                        "specialist_name": result.get("specialist_name", "Специалист"),
                        "time": time
                    }

                    if relation == "today":
                        response["day"] = "сегодня"
                        response["day_kz"] = "бүгін"
                    elif relation == "tomorrow":
                        response["day"] = "завтра"
                        response["day_kz"] = "ертең"

                    return response

                # Error case with alternative times
                elif result.get("status") in ["suggest_times", "error_change_reception"] or \
                        result.get("status", "").startswith("error_change_reception"):

                    # Extract alternative times
                    available_times = []
                    if "suggested_times" in result and isinstance(result["suggested_times"], list):
                        available_times = result["suggested_times"]
                    elif "all_available_times" in result and isinstance(result["all_available_times"], list):
                        available_times = result["all_available_times"]
                    else:
                        # Check standard time fields
                        for key in ["first_time", "second_time", "third_time"]:
                            if key in result and result[key]:
                                available_times.append(result[key])

                        # Check numeric time fields
                        for i in range(1, 10):
                            key = f"time_{i}"
                            if key in result and result[key]:
                                available_times.append(result[key])

                    # Clean times (remove date part, seconds)
                    clean_times = []
                    for t in available_times:
                        if isinstance(t, str):
                            if " " in t:  # Format: "YYYY-MM-DD HH:MM"
                                clean_times.append(t.split(" ")[1])
                            else:
                                # Remove seconds if present
                                if t.count(":") == 2:  # Format: "HH:MM:SS"
                                    clean_times.append(":".join(t.split(":")[:2]))
                                else:
                                    clean_times.append(t)

                    # No alternative times
                    if not clean_times:
                        return {
                            "status": "error_change_reception_bad_date",
                            "data": result.get("message", "Ошибка изменения даты приема")
                        }

                    # One alternative time
                    elif len(clean_times) == 1:
                        status = "change_only_first_time"
                        if relation == "today":
                            status = "change_only_first_time_today"
                        elif relation == "tomorrow":
                            status = "change_only_first_time_tomorrow"

                        response = {
                            "status": status,
                            **date_info,
                            "specialist_name": result.get("specialist_name", "Специалист"),
                            "first_time": clean_times[0]
                        }

                        if relation == "today":
                            response["day"] = "сегодня"
                            response["day_kz"] = "бүгін"
                        elif relation == "tomorrow":
                            response["day"] = "завтра"
                            response["day_kz"] = "ертең"

                        return response

                    # Two alternative times
                    elif len(clean_times) == 2:
                        status = "change_only_two_time"
                        if relation == "today":
                            status = "change_only_two_time_today"
                        elif relation == "tomorrow":
                            status = "change_only_two_time_tomorrow"

                        response = {
                            "status": status,
                            **date_info,
                            "specialist_name": result.get("specialist_name", "Специалист"),
                            "first_time": clean_times[0],
                            "second_time": clean_times[1]
                        }

                        if relation == "today":
                            response["day"] = "сегодня"
                            response["day_kz"] = "бүгін"
                        elif relation == "tomorrow":
                            response["day"] = "завтра"
                            response["day_kz"] = "ертең"

                        return response

                    # Three or more alternative times
                    else:
                        status = "error_change_reception"
                        if relation == "today":
                            status = "error_change_reception_today"
                        elif relation == "tomorrow":
                            status = "error_change_reception_tomorrow"

                        response = {
                            "status": status,
                            **date_info,
                            "specialist_name": result.get("specialist_name", "Специалист"),
                            "first_time": clean_times[0],
                            "second_time": clean_times[1],
                            "third_time": clean_times[2]
                        }

                        if relation == "today":
                            response["day"] = "сегодня"
                            response["day_kz"] = "бүгін"
                        elif relation == "tomorrow":
                            response["day"] = "завтра"
                            response["day_kz"] = "ертең"

                        return response

                # Bad date error
                elif result.get("status") == "error_change_reception_bad_date":
                    return {
                        "status": "error_change_reception_bad_date",
                        "data": result.get("message", "Ошибка изменения даты приема")
                    }

                # Non-working time
                elif result.get("status") == "nonworktime":
                    return {"status": "nonworktime"}

            elif function_name == "delete_reception_for_patient":
                if result.get("status") == "success_delete":
                    return {
                        "status": "success_deleting_reception",
                        "message": "Запись успешно удалена"
                    }
                else:
                    return {
                        "status": "error_deleting_reception",
                        "message": result.get("message", "Ошибка при удалении записи")
                    }

            elif function_name == "appointment_time_for_patient":
                if result.get("status") == "error_no_appointment":
                    return {
                        "status": "error_reception_unavailable",
                        "message": "У пациента нет активных записей на прием"
                    }

                if "appointment_time" in result and "appointment_date" in result:
                    time = result["appointment_time"]

                    # Parse appointment date
                    appt_date_obj = None
                    try:
                        appt_date_obj = datetime.strptime(result["appointment_date"], "%Y-%m-%d")
                    except ValueError:
                        pass

                    if appt_date_obj:
                        day = appt_date_obj.day
                        month_num = appt_date_obj.month
                        weekday_idx = appt_date_obj.weekday()

                        # Determine if today or tomorrow
                        today = datetime.now().date()
                        tomorrow = today + timedelta(days=1)

                        if appt_date_obj.date() == today:
                            day_ru = "сегодня"
                            day_kz = "бүгін"
                        elif appt_date_obj.date() == tomorrow:
                            day_ru = "завтра"
                            day_kz = "ертең"
                        else:
                            day_ru = None
                            day_kz = None

                        response = {
                            "status": "success_for_check_info",
                            "date": f"{day} {MONTHS_RU[month_num]}",
                            "date_kz": f"{day} {MONTHS_KZ[month_num]}",
                            "specialist_name": result.get("doctor_name", "Специалист"),
                            "weekday": WEEKDAYS_RU[weekday_idx],
                            "weekday_kz": WEEKDAYS_KZ[weekday_idx],
                            "time": time
                        }

                        if day_ru:
                            response["day"] = day_ru
                            response["day_kz"] = day_kz

                        return response

            # If we couldn't format properly, pass through the original result
            # but ensure it has a reasonable status
            if "status" not in result:
                # Try to infer a meaningful status
                if function_name == "which_time_in_certain_day":
                    result["status"] = "which_time"
                elif function_name == "reserve_reception_for_patient":
                    result["status"] = "success_change_reception"
                elif function_name == "delete_reception_for_patient":
                    result["status"] = "success_deleting_reception"
                elif function_name == "appointment_time_for_patient":
                    result["status"] = "success_for_check_info"

            return result

        except Exception as e:
            logger.error(f"Error formatting ACS response: {e}", exc_info=True)
            return {
                "status": "error_med_element",
                "message": "Ошибка форматирования ответа"
            }

    def get_messages(self, thread_id: str, limit: int = 10) -> List[dict]:
        """
        Gets messages from a thread.

        Args:
            thread_id: Thread ID
            limit: Maximum number of messages to retrieve

        Returns:
            List[dict]: Message list
        """
        try:
            messages = self.client.beta.threads.messages.list(
                thread_id=thread_id,
                limit=limit,
                order="desc"  # Get newest messages first
            )
            logger.info(f"Retrieved {len(messages.data)} messages from thread {thread_id}")
            return messages.data
        except Exception as e:
            logger.error(f"Error retrieving messages: {e}")
            return []

    def _cancel_run(self, thread_id: str, run_id: str) -> None:
        """
        Cancels an active run.

        Args:
            thread_id: Thread ID
            run_id: Run ID
        """
        try:
            # Check current run status first
            run = self.client.beta.threads.runs.retrieve(
                thread_id=thread_id,
                run_id=run_id
            )

            # Only attempt to cancel if run is in a cancelable state
            if run.status in ["in_progress", "queued", "requires_action"]:
                self.client.beta.threads.runs.cancel(
                    thread_id=thread_id,
                    run_id=run_id
                )
                logger.info(f"Successfully cancelled run {run_id}")
            else:
                logger.info(f"Run {run_id} already in terminal state: {run.status}, no need to cancel")

            # Update status in database
            self._update_run_status(run_id, "cancelled")
        except Exception as e:
            logger.error(f"Error cancelling run: {e}")

    def _update_run_status(self, run_id: str, status: str) -> None:
        """
        Updates run status in database.

        Args:
            run_id: Run ID
            status: New status
        """
        try:
            run_model = RunModel.objects.filter(run_id=run_id).first()
            if run_model and run_model.status != status:
                run_model.status = status
                run_model.save()
                logger.info(f"Updated run {run_id} status to: {status}")
        except Exception as e:
            logger.error(f"Error updating run status: {e}")

    def create_fallback_response(user_input, patient_code, appointment=None):
        """
        Creates a fallback response when the main processing flow fails.
        Makes direct function calls based on user input to ensure a meaningful response.

        Args:
            user_input: The user's input text
            patient_code: The patient's ID code
            appointment: Optional appointment object

        Returns:
            JsonResponse: A properly formatted response
        """
        from reminder.openai_assistant.api_views import process_which_time_response, process_reserve_reception_response, \
            process_delete_reception_response

        try:
            user_input = user_input.lower()

            # Check for appointment info request (highest priority for clarity)
            if any(word in user_input for word in ["когда", "какая", "время", "записан", "во сколько", "не помню"]):
                result = appointment_time_for_patient(patient_code)

            # Check for cancel request
            elif any(word in user_input for word in ["отмен", "удал", "не приду", "убер"]):
                if not any(word in user_input for word in ["перенес", "перезапиш", "измен"]):
                    result = delete_reception_for_patient(patient_code)

                    if hasattr(result, 'content'):
                        result_dict = json.loads(result.content.decode('utf-8'))
                        processed_result = process_delete_reception_response(result_dict)
                        return JsonResponse(processed_result)
                    else:
                        processed_result = process_delete_reception_response(result)
                        return JsonResponse(processed_result)

            # Check for booking/rescheduling request
            elif any(word in user_input for word in ["запиш", "записаться", "перенес", "перезапиш", "измен"]):
                # First check if patient exists
                try:
                    patient = Patient.objects.get(patient_code=patient_code)
                except Patient.DoesNotExist:
                    return JsonResponse({
                        "status": "error_med_element",
                        "message": f"Пациент с кодом {patient_code} не найден"
                    })

                # Determine date from request
                date_obj = datetime.now()
                if "завтра" in user_input:
                    date_obj = datetime.now() + timedelta(days=1)
                elif "послезавтра" in user_input or "после завтра" in user_input:
                    date_obj = datetime.now() + timedelta(days=2)

                # Check if time is explicitly specified
                time_str = None
                time_pattern = r'(\d{1,2})[:\s](\d{2})'
                time_match = re.search(time_pattern, user_input)
                if time_match:
                    hour = int(time_match.group(1))
                    minute = int(time_match.group(2))
                    time_str = f"{hour:02d}:{minute:02d}"
                else:
                    # Use predefined times based on time of day mentions
                    if "утр" in user_input:
                        time_str = "10:30"
                    elif "обед" in user_input or "днем" in user_input or "в обед" in user_input:
                        time_str = "13:30"
                    elif "вечер" in user_input or "ужин" in user_input:
                        time_str = "18:30"
                    else:
                        # If no time specified, check available slots from the database
                        date_only = date_obj.date()
                        available_slots = AvailableTimeSlot.objects.filter(
                            patient=patient,
                            date=date_only
                        ).order_by('time')

                        if available_slots.exists():
                            # Use the first available slot
                            slot = available_slots.first()
                            time_str = slot.time.strftime("%H:%M")
                        else:
                            # If no slots available in DB, try to get them first
                            try:
                                # Get available times for the day
                                date_str = date_obj.strftime("%Y-%m-%d")
                                available_times = which_time_in_certain_day(patient_code, date_str)

                                # Process the result to extract times
                                if hasattr(available_times, 'content'):
                                    available_times_dict = json.loads(available_times.content.decode('utf-8'))
                                else:
                                    available_times_dict = available_times

                                # Extract first time if available
                                if ("first_time" in available_times_dict and available_times_dict["first_time"] or
                                        "time_1" in available_times_dict and available_times_dict["time_1"]):
                                    time_str = available_times_dict.get("first_time") or available_times_dict.get("time_1")
                                # If no times available, notify user
                                else:
                                    return JsonResponse({
                                        "status": "error_empty_windows",
                                        "message": f"На {date_obj.strftime('%d.%m.%Y')} нет доступных окон для записи."
                                    })
                            except Exception as e:
                                logger.error(f"Error getting available times: {e}")
                                # Default time if all else fails
                                time_str = "10:30"

                # Format date and time for booking
                date_str = date_obj.strftime("%Y-%m-%d")
                datetime_str = f"{date_str} {time_str}"

                # Call function
                result = reserve_reception_for_patient(patient_code, datetime_str, 1)

                if hasattr(result, 'content'):
                    result_dict = json.loads(result.content.decode('utf-8'))
                    processed_result = process_reserve_reception_response(result_dict, date_obj, time_str)
                    return JsonResponse(processed_result)
                else:
                    processed_result = process_reserve_reception_response(result, date_obj, time_str)
                    return JsonResponse(processed_result)

            # Check for available time request
            elif any(word in user_input for word in ["свобод", "доступн", "окошк", "када", "когда можно"]):
                # Determine date from request
                if "завтра" in user_input:
                    date_obj = datetime.now() + timedelta(days=1)
                elif "послезавтра" in user_input or "после завтра" in user_input:
                    date_obj = datetime.now() + timedelta(days=2)
                else:
                    date_obj = datetime.now()  # Default to today

                date_str = date_obj.strftime("%Y-%m-%d")

                # Call function
                result = which_time_in_certain_day(patient_code, date_str)

                if hasattr(result, 'content'):
                    result_dict = json.loads(result.content.decode('utf-8'))
                    processed_result = process_which_time_response(result_dict, date_obj)
                    return JsonResponse(processed_result)
                else:
                    processed_result = process_which_time_response(result, date_obj)
                    return JsonResponse(processed_result)

            # Default to checking available times for today
            else:
                date_obj = datetime.now()
                date_str = date_obj.strftime("%Y-%m-%d")

                result = which_time_in_certain_day(patient_code, date_str)

                if hasattr(result, 'content'):
                    result_dict = json.loads(result.content.decode('utf-8'))
                    processed_result = process_which_time_response(result_dict, date_obj)
                    return JsonResponse(processed_result)
                else:
                    processed_result = process_which_time_response(result, date_obj)
                    return JsonResponse(processed_result)

        except Exception as e:
            logger.error(f"Error in fallback response generation: {e}", exc_info=True)
            return JsonResponse({
                "status": "error_med_element",
                "message": "Произошла ошибка при обработке запроса"
            })
