import os
import re
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Union
from django.utils import timezone
from django.conf import settings
from openai import OpenAI
from reminder.openai_assistant.response_formatter import (
    format_booking_response, format_available_times_response,
    MONTHS_RU, MONTHS_KZ, WEEKDAYS_RU, WEEKDAYS_KZ
)
from reminder.models import Assistant, Thread, Run as RunModel, Patient, Appointment, QueueInfo
from reminder.openai_assistant.assistant_instructions import get_enhanced_assistant_prompt

logger = logging.getLogger(__name__)

# Определение карты соответствий времени суток к конкретным часам
TIME_MAPPINGS = {
    # Утро
    "утро": "10:30",
    "утром": "10:30",
    "с утра": "10:30",
    "на утро": "10:30",
    "пораньше": "10:30",
    "рано": "10:30",
    "раннее": "10:30",

    # Обед
    "обед": "13:30",
    "на обед": "13:30",
    "в обед": "13:30",
    "полдень": "13:30",
    "в полдень": "13:30",
    "дневное": "13:30",
    "днем": "13:30",

    # После обеда
    "после обеда": "15:00",
    "послеобеденное": "15:00",
    "дневное время": "15:00",

    # До обеда
    "до обеда": "11:00",
    "перед обедом": "11:00",
    "предобеденное": "11:00",

    # Вечер
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


class AssistantClient:
    """
    Улучшенный клиент для работы с OpenAI Assistant API.
    Предоставляет интерфейс для взаимодействия пациентов с системой записи на прием.
    """

    def __init__(self):
        """Инициализация клиента с ключом API из настроек."""
        self.client = OpenAI(api_key=settings.OPENAI_API_KEY)

    def get_or_create_thread(self, appointment_id: int) -> Thread:
        """
        Gets an existing thread or creates a new one for a dialog with a patient.
        Enhanced with better error handling for stuck threads.

        Args:
            appointment_id: Appointment ID

        Returns:
            Thread: Thread object for the dialog
        """
        try:
            # Check if appointment exists
            appointment = Appointment.objects.get(appointment_id=appointment_id)

            # Check if there's an active thread for this appointment
            existing_thread = Thread.objects.filter(
                appointment_id=appointment_id,
                expires_at__gt=timezone.now()
            ).first()

            if existing_thread:
                logger.info(f"Found existing thread {existing_thread.thread_id} for appointment {appointment_id}")

                # Before returning, check if this thread has an active run by making a call to the API
                try:
                    # Get the most recent run for this thread
                    runs = self.client.beta.threads.runs.list(
                        thread_id=existing_thread.thread_id,
                        limit=1
                    )

                    # If there are runs and the latest one is still active
                    if runs.data and runs.data[0].status in ["in_progress", "queued", "requires_action"]:
                        logger.warning(
                            f"Thread {existing_thread.thread_id} has an active run {runs.data[0].id}. Creating a new thread.")

                        # Instead of trying to cancel (which can fail), create a new thread
                        return self._create_fresh_thread(appointment)

                    # Thread is free, we can use it
                    return existing_thread

                except Exception as e:
                    logger.warning(f"Error checking run status for thread {existing_thread.thread_id}: {e}")
                    # If we can't check the run status, create a new thread to be safe
                    return self._create_fresh_thread(appointment)

            # No active thread found, create a new one
            return self._create_fresh_thread(appointment)

        except Appointment.DoesNotExist:
            logger.error(f"Appointment with ID {appointment_id} not found")
            raise ValueError(f"Appointment with ID {appointment_id} not found")
        except Exception as e:
            logger.error(f"Error in get_or_create_thread: {e}", exc_info=True)
            raise

    def _create_fresh_thread(self, appointment: Appointment) -> Thread:
        """
        Creates a completely new thread for an appointment.

        Args:
            appointment: Appointment object

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

            # Save thread in local database
            thread = Thread.objects.create(
                thread_id=openai_thread.id,
                order_key=str(appointment.appointment_id),
                assistant=assistant,
                appointment_id=appointment.appointment_id
            )

            logger.info(f"Created new thread {thread.thread_id} for appointment {appointment.appointment_id}")
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
                logger.warning(f"Thread {thread_id} has an active run, cannot add message. Creating a new thread.")

                # Find the appointment ID from the Thread object
                try:
                    thread_obj = Thread.objects.get(thread_id=thread_id)
                    appointment_id = thread_obj.appointment_id

                    # Get the appointment
                    appointment = Appointment.objects.get(appointment_id=appointment_id)

                    # Create a completely new thread
                    new_thread = self._create_fresh_thread(appointment)

                    # Now add the message to the new thread
                    message = self.client.beta.threads.messages.create(
                        thread_id=new_thread.thread_id,
                        role=role,
                        content=content
                    )

                    logger.info(f"Added message to new thread {new_thread.thread_id}")

                    # Update the original Thread object to use the new thread_id
                    thread_obj.thread_id = new_thread.thread_id
                    thread_obj.save()

                    return message
                except Exception as inner_e:
                    logger.error(f"Error creating new thread when active run detected: {inner_e}")
                    raise

            # For other errors
            logger.error(f"Error adding message to thread: {e}")
            raise

    def run_assistant(self, thread: Thread, appointment: Appointment, instructions: str = None) -> RunModel:
        """
        Runs the assistant with proper instructions to ensure function calling.
        Enhanced with better error handling for active runs.

        Args:
            thread: Thread object
            appointment: Appointment object
            instructions: Additional instructions to enforce function calling

        Returns:
            RunModel: Run model object
        """
        try:
            # First check if this thread already has an active run
            runs = self.client.beta.threads.runs.list(
                thread_id=thread.thread_id,
                limit=1
            )

            # If there's an active run, create a new thread
            if runs.data and runs.data[0].status in ["in_progress", "queued", "requires_action"]:
                logger.warning(f"Thread {thread.thread_id} already has an active run. Creating a new thread.")

                # Create a new thread
                new_thread = self._create_fresh_thread(appointment)

                # Update the original Thread object
                thread.thread_id = new_thread.thread_id
                thread.save()

                # Continue with the new thread ID
                thread_id = new_thread.thread_id
            else:
                thread_id = thread.thread_id

            # Get patient and appointment data
            patient = appointment.patient
            patient_code = patient.patient_code

            # Format appointment time
            appointment_time = "Не указано"
            if appointment.start_time:
                appointment_time = appointment.start_time.strftime("%Y-%m-%d %H:%M")

            # Get doctor name
            doctor_name = "Не указан"
            if appointment.doctor:
                doctor_name = appointment.doctor.full_name

            # Get clinic name
            clinic_name = "Не указана"
            if appointment.clinic:
                clinic_name = appointment.clinic.name

            # Get current Moscow date and time (Moscow is UTC+3)
            from datetime import timezone as tz
            moscow_tz = tz(timedelta(hours=3))
            current_moscow_datetime = datetime.now(moscow_tz)

            # Format current Moscow date and time
            current_moscow_date = current_moscow_datetime.strftime("%Y-%m-%d")
            current_moscow_time = current_moscow_datetime.strftime("%H:%M")
            current_moscow_full = current_moscow_datetime.strftime("%Y-%m-%d %H:%M")

            # Get today's and tomorrow's dates in Moscow time
            today = current_moscow_datetime.strftime("%Y-%m-%d")
            tomorrow = (current_moscow_datetime + timedelta(days=1)).strftime("%Y-%m-%d")

            # Calculate dates for "after tomorrow" and "a week later"
            after_tomorrow = (current_moscow_datetime + timedelta(days=2)).strftime("%Y-%m-%d")
            week_later = (current_moscow_datetime + timedelta(days=7)).strftime("%Y-%m-%d")

            # Get day of week in Russian
            weekdays_ru = {
                0: "Понедельник",
                1: "Вторник",
                2: "Среда",
                3: "Четверг",
                4: "Пятница",
                5: "Суббота",
                6: "Воскресенье"
            }
            current_weekday = weekdays_ru[current_moscow_datetime.weekday()]

            # Use provided instructions or create default
            if not instructions:
                instructions = """
                # КРИТИЧЕСКИ ВАЖНО: ВСЕГДА ИСПОЛЬЗУЙ ФУНКЦИИ, А НЕ ТЕКСТОВЫЕ ОТВЕТЫ!

                В следующих ситуациях ты ОБЯЗАТЕЛЬНО должен вызвать функцию вместо текстового ответа:

                1. Когда пользователь спрашивает о свободных окошках или времени:
                   → ВСЕГДА вызывай функцию which_time_in_certain_day с параметрами:
                      - patient_code: "{patient_code}"
                      - date_time: "today" или "tomorrow" или конкретная дата в формате YYYY-MM-DD

                2. Когда пользователь интересуется своей текущей записью:
                   → ВСЕГДА вызывай функцию appointment_time_for_patient с параметрами:
                      - patient_code: "{patient_code}"

                3. Когда пользователь хочет записаться или перенести запись:
                   → ВСЕГДА вызывай функцию reserve_reception_for_patient с параметрами:
                      - patient_id: "{patient_code}"
                      - date_from_patient: конкретная дата и время в формате "YYYY-MM-DD HH:MM"
                      - trigger_id: 1 для записи, 2 для поиска ближайших свободных времен

                4. Когда пользователь хочет отменить запись:
                   → ВСЕГДА вызывай функцию delete_reception_for_patient с параметрами:
                      - patient_id: "{patient_code}"

                # ЗАПРЕЩЕНО использовать текстовые ответы для вышеперечисленных запросов!
                # ВСЕГДА вызывай соответствующую функцию!
                """

            instructions = instructions.format(
                patient_code=patient_code,
                today=today,
                tomorrow=tomorrow
            )

            # Add info about the patient and appointment
            patient_info = f"""
            # ВАЖНАЯ ИНФОРМАЦИЯ О ПАЦИЕНТЕ И ПРИЕМЕ:

            - Текущий пациент: {patient.full_name} (ID: {patient_code})
            - Запись на прием: {appointment.appointment_id}
            - Время приема: {appointment_time}
            - Врач: {doctor_name}
            - Клиника: {clinic_name}
            """

            # Add current date and time context
            datetime_context = f"""
            # ТЕКУЩАЯ ДАТА И ВРЕМЯ (Москва):

            - Текущая дата: {current_moscow_date} ({current_weekday})
            - Текущее время: {current_moscow_time}
            - Полная дата и время: {current_moscow_full}

            # ИНТЕРПРЕТАЦИЯ ДАТ:
            - Сегодня: {today}
            - Завтра: {tomorrow}
            - Послезавтра: {after_tomorrow}
            - Через неделю: {week_later}

            Если пользователь спрашивает о "послезавтра", это {after_tomorrow}.
            Если пользователь спрашивает о "после после завтра", это {after_tomorrow}.
            Если пользователь спрашивает о "через неделю", это {week_later}.

            Используй эту информацию для определения даты в запросах.
            """

            # Combine all instructions
            full_instructions = instructions + "\n\n" + patient_info + "\n\n" + datetime_context

            # Create assistant run with combined instructions
            try:
                openai_run = self.client.beta.threads.runs.create(
                    thread_id=thread_id,
                    assistant_id=thread.assistant.assistant_id,
                    instructions=full_instructions
                )

                # Save run in database
                run = RunModel.objects.create(
                    run_id=openai_run.id,
                    status=openai_run.status
                )

                # Update thread with current run
                thread.current_run = run
                thread.save()

                logger.info(
                    f"Started run {run.run_id} for thread {thread_id} with strict function calling instructions (Moscow time: {current_moscow_full})")
                return run
            except Exception as run_error:
                # If we get an error about an active run, try creating a new thread again
                error_message = str(run_error)
                if "already has an active run" in error_message:
                    logger.warning(
                        f"Thread {thread_id} already has an active run despite our check. Creating a new thread and trying again.")

                    # Create a new thread one more time
                    new_thread = self._create_fresh_thread(appointment)

                    # Update the original Thread object
                    thread.thread_id = new_thread.thread_id
                    thread.save()

                    # Try creating the run with the new thread
                    openai_run = self.client.beta.threads.runs.create(
                        thread_id=new_thread.thread_id,
                        assistant_id=thread.assistant.assistant_id,
                        instructions=full_instructions
                    )

                    # Save run in database
                    run = RunModel.objects.create(
                        run_id=openai_run.id,
                        status=openai_run.status
                    )

                    # Update thread with current run
                    thread.current_run = run
                    thread.save()

                    logger.info(
                        f"Started run {run.run_id} for new thread {new_thread.thread_id} after active run error (Moscow time: {current_moscow_full})")
                    return run
                else:
                    # For other errors, just raise
                    raise

        except Exception as e:
            logger.error(f"Error running assistant: {e}", exc_info=True)
            raise

    def handle_function_calls(self, run_id: str, thread_id: str) -> Any:
        """
        Обрабатывает функциональные вызовы от ассистента и форматирует ответы
        в соответствии с требованиями ACS.
        Исправлено для возврата первого форматированного результата сразу.

        Args:
            run_id: ID запуска
            thread_id: ID треда

        Returns:
            Any: Результат обработки вызовов функций
        """
        try:
            run = self.client.beta.threads.runs.retrieve(
                thread_id=thread_id,
                run_id=run_id
            )

            # Если требуется действие
            if run.status == "requires_action" and run.required_action:
                tool_calls = run.required_action.submit_tool_outputs.tool_calls
                logger.info(f"Обнаружено {len(tool_calls)} вызовов функций")

                tool_outputs = []
                formatted_results = []

                for tool_call in tool_calls:
                    function_name = tool_call.function.name
                    function_args = json.loads(tool_call.function.arguments)

                    logger.info(f"Обнаружен вызов функции: {function_name} с аргументами: {function_args}")

                    # Получаем результат вызова функции
                    raw_result = self._call_function(function_name, function_args, thread_id)
                    logger.info(f"Сырой результат функции: {raw_result}")

                    # Форматируем результат для ACS
                    formatted_result = self._format_for_acs(function_name, function_args, raw_result)
                    logger.info(f"Отформатированный результат для ACS: {formatted_result}")

                    # Сохраняем форматированный результат
                    formatted_results.append(formatted_result)

                    tool_outputs.append({
                        "tool_call_id": tool_call.id,
                        "output": json.dumps(formatted_result)
                    })

                    # ВАЖНОЕ ИЗМЕНЕНИЕ: Возвращаем первый форматированный результат сразу
                    # Это предотвратит продолжение выполнения и создания текстового ответа
                    if formatted_result and "status" in formatted_result:
                        logger.info(
                            f"Немедленно возвращаем форматированный результат со статусом {formatted_result['status']}")

                        # Мы все равно отправляем результаты вызовов функций, чтобы не оставлять выполнение в подвешенном состоянии
                        self.client.beta.threads.runs.submit_tool_outputs(
                            thread_id=thread_id,
                            run_id=run_id,
                            tool_outputs=tool_outputs
                        )

                        # Но не ждем завершения текстового ответа, а сразу возвращаем результат функции
                        return formatted_result

                # Если мы дошли до этой точки, значит у нас есть несколько результатов функций
                # Отправляем результаты вызовов функций
                if tool_outputs:
                    self.client.beta.threads.runs.submit_tool_outputs(
                        thread_id=thread_id,
                        run_id=run_id,
                        tool_outputs=tool_outputs
                    )
                    logger.info(f"Отправлено {len(tool_outputs)} результатов функций для запуска {run_id}")
                    # Возвращаем первый результат, если он есть
                    if formatted_results:
                        return formatted_results[0]
                else:
                    logger.warning("Нет результатов функций для отправки")
                    return {"status": "error_med_element", "message": "Не получены результаты функций"}

            return run.status

        except Exception as e:
            logger.error(f"Ошибка при обработке вызовов функций: {str(e)}", exc_info=True)
            return {"status": "error_med_element", "message": f"Ошибка обработки: {str(e)}"}

    def _format_for_acs(self, function_name: str, function_args: dict, result: dict) -> dict:
        """
        Formats function results for ACS according to required status formats
        with additional validation to ensure format compliance.
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

            # Format date information
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

                # Determine if today or tomorrow
                today = datetime.now().date()
                tomorrow = today + timedelta(days=1)

                if date_obj.date() == today:
                    relation = "today"
                    date_info["day"] = "сегодня"
                    date_info["day_kz"] = "бүгін"
                elif date_obj.date() == tomorrow:
                    relation = "tomorrow"
                    date_info["day"] = "завтра"
                    date_info["day_kz"] = "ертең"
                else:
                    relation = None
            else:
                date_info = {}
                relation = None

            # Which time in certain day function (for available times)
            if function_name == "which_time_in_certain_day":
                available_times = []

                # Extract available times from result
                if "all_available_times" in result and isinstance(result["all_available_times"], list):
                    available_times = result["all_available_times"]
                elif "suggested_times" in result and isinstance(result["suggested_times"], list):
                    available_times = result["suggested_times"]
                elif "first_time" in result:
                    if result.get("first_time"): available_times.append(result["first_time"])
                    if "second_time" in result and result.get("second_time"): available_times.append(
                        result["second_time"])
                    if "third_time" in result and result.get("third_time"): available_times.append(result["third_time"])

                # Clean times (remove date part, seconds)
                cleaned_times = []
                for t in available_times:
                    if isinstance(t, str):
                        if " " in t:  # Format: "YYYY-MM-DD HH:MM"
                            t = t.split(" ")[1]
                        if t.count(":") == 2:  # Format: "HH:MM:SS"
                            t = ":".join(t.split(":")[:2])
                        cleaned_times.append(t)

                # No available times
                if not cleaned_times:
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

                # Only one time available
                elif len(cleaned_times) == 1:
                    status = "only_first_time"
                    if relation == "today":
                        status = "only_first_time_today"
                    elif relation == "tomorrow":
                        status = "only_first_time_tomorrow"

                    response = {
                        "status": status,
                        **date_info,
                        "specialist_name": result.get("specialist_name", "Специалист"),
                        "first_time": cleaned_times[0]
                    }

                    return response

                # Only two times available
                elif len(cleaned_times) == 2:
                    status = "only_two_time"
                    if relation == "today":
                        status = "only_two_time_today"
                    elif relation == "tomorrow":
                        status = "only_two_time_tomorrow"

                    response = {
                        "status": status,
                        **date_info,
                        "specialist_name": result.get("specialist_name", "Специалист"),
                        "first_time": cleaned_times[0],
                        "second_time": cleaned_times[1]
                    }

                    return response

                # Three or more times available
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
                        "first_time": cleaned_times[0],
                        "second_time": cleaned_times[1],
                        "third_time": cleaned_times[2]
                    }

                    return response

            # Reserve reception for patient (booking/rescheduling)
            elif function_name == "reserve_reception_for_patient":
                # Success case
                if result.get("status") == "success_schedule" or result.get("status", "").startswith(
                        "success_change_reception"):
                    time = result.get("time", "")
                    if " " in time:  # Format: "YYYY-MM-DD HH:MM"
                        time = time.split(" ")[1]
                    if time.count(":") == 2:  # Format: "HH:MM:SS"
                        time = ":".join(time.split(":")[:2])

                    status = "success_change_reception"
                    if relation == "today":
                        status = "success_change_reception_today"
                    elif relation == "tomorrow":
                        status = "success_change_reception_tomorrow"

                    response = {
                        "status": status,
                        **date_info,
                        "specialist_name": result.get("specialist_name", "Специалист"),
                        "time": time
                    }

                    return response

                # Error case with alternative times
                elif result.get("status") == "suggest_times" or result.get("status", "").startswith(
                        "error_change_reception"):
                    available_times = []

                    # Extract available times
                    if "suggested_times" in result and isinstance(result["suggested_times"], list):
                        available_times = result["suggested_times"]

                    # Clean times
                    cleaned_times = []
                    for t in available_times:
                        if isinstance(t, str):
                            if " " in t:  # Format: "YYYY-MM-DD HH:MM"
                                t = t.split(" ")[1]
                            if t.count(":") == 2:  # Format: "HH:MM:SS"
                                t = ":".join(t.split(":")[:2])
                            cleaned_times.append(t)

                    # No alternative times
                    if not cleaned_times:
                        status = "error_change_reception_bad_date"
                        return {
                            "status": status,
                            "data": result.get("message", "Ошибка изменения даты приема")
                        }

                    # One alternative time
                    elif len(cleaned_times) == 1:
                        status = "change_only_first_time"
                        if relation == "today":
                            status = "change_only_first_time_today"
                        elif relation == "tomorrow":
                            status = "change_only_first_time_tomorrow"

                        response = {
                            "status": status,
                            **date_info,
                            "specialist_name": result.get("specialist_name", "Специалист"),
                            "first_time": cleaned_times[0]
                        }

                        return response

                    # Two alternative times
                    elif len(cleaned_times) == 2:
                        status = "change_only_two_time"
                        if relation == "today":
                            status = "change_only_two_time_today"
                        elif relation == "tomorrow":
                            status = "change_only_two_time_tomorrow"

                        response = {
                            "status": status,
                            **date_info,
                            "specialist_name": result.get("specialist_name", "Специалист"),
                            "first_time": cleaned_times[0],
                            "second_time": cleaned_times[1]
                        }

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
                            "first_time": cleaned_times[0],
                            "second_time": cleaned_times[1],
                            "third_time": cleaned_times[2]
                        }

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

                # Unknown error
                else:
                    return {
                        "status": "error_med_element",
                        "message": result.get("message", "Ошибка медицинской системы")
                    }

            # Delete reception for patient (cancellation)
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

            # Appointment time for patient (current appointment info)
            elif function_name == "appointment_time_for_patient":
                if result.get("status") == "error_no_appointment":
                    return {
                        "status": "error_reception_unavailable",
                        "message": "У пациента нет активных записей на прием"
                    }

                if "appointment_time" in result and "appointment_date" in result:
                    time = result["appointment_time"]

                    # Try to parse appointment date
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

            # If we couldn't format properly, return original result with modifications
            # Make sure at least the status is in a reasonable format
            if "status" not in result or result["status"] == "success":
                # Try to guess appropriate status
                if function_name == "which_time_in_certain_day":
                    result["status"] = "which_time"
                    if relation == "today":
                        result["status"] = "which_time_today"
                    elif relation == "tomorrow":
                        result["status"] = "which_time_tomorrow"
                elif function_name == "reserve_reception_for_patient":
                    result["status"] = "success_change_reception"
                    if relation == "today":
                        result["status"] = "success_change_reception_today"
                    elif relation == "tomorrow":
                        result["status"] = "success_change_reception_tomorrow"
                elif function_name == "delete_reception_for_patient":
                    result["status"] = "success_deleting_reception"
                    if "message" not in result:
                        result["message"] = "Запись успешно удалена"
                elif function_name == "appointment_time_for_patient":
                    result["status"] = "success_for_check_info"

            # Add date info if missing and available
            if date_info and isinstance(result, dict):
                for key, value in date_info.items():
                    if key not in result:
                        result[key] = value

            return result

        except Exception as e:
            logger.error(f"Error formatting ACS response: {e}", exc_info=True)
            # Return a safe fallback response
            return {
                "status": "error_med_element",
                "message": "Ошибка форматирования ответа"
            }

            def format_date_info(date_str):
                """Formats date for ACS response"""
                try:
                    # Parse date string
                    if date_str == "today":
                        date_obj = datetime.now()
                    elif date_str == "tomorrow":
                        date_obj = datetime.now() + timedelta(days=1)
                    else:
                        if ' ' in date_str:  # Has time
                            date_str = date_str.split(' ')[0]
                        date_obj = datetime.strptime(date_str, "%Y-%m-%d")

                    # Month mappings
                    months_ru = {
                        1: "Января", 2: "Февраля", 3: "Марта", 4: "Апреля", 5: "Мая", 6: "Июня",
                        7: "Июля", 8: "Августа", 9: "Сентября", 10: "Октября", 11: "Ноября", 12: "Декабря"
                    }

                    months_kz = {
                        1: "Қаңтар", 2: "Ақпан", 3: "Наурыз", 4: "Сәуір", 5: "Мамыр", 6: "Маусым",
                        7: "Шілде", 8: "Тамыз", 9: "Қыркүйек", 10: "Қазан", 11: "Қараша", 12: "Желтоқсан"
                    }

                    weekdays_ru = {
                        0: "Понедельник", 1: "Вторник", 2: "Среда",
                        3: "Четверг", 4: "Пятница", 5: "Суббота", 6: "Воскресенье"
                    }

                    weekdays_kz = {
                        0: "Дүйсенбі", 1: "Сейсенбі", 2: "Сәрсенбі",
                        3: "Бейсенбі", 4: "Жұма", 5: "Сенбі", 6: "Жексенбі"
                    }

                    return {
                        "date": f"{date_obj.day} {months_ru[date_obj.month]}",
                        "date_kz": f"{date_obj.day} {months_kz[date_obj.month]}",
                        "weekday": weekdays_ru[date_obj.weekday()],
                        "weekday_kz": weekdays_kz[date_obj.weekday()],
                    }
                except Exception as e:
                    logger.error(f"Error formatting date: {e}")
                    return {
                        "date": "Неизвестная дата",
                        "date_kz": "Белгісіз күн",
                        "weekday": "Неизвестный день",
                        "weekday_kz": "Белгісіз күн",
                    }

            # For which_time_in_certain_day function
            if function_name == "which_time_in_certain_day":
                date_time = function_args.get("date_time", "")
                date_relation = get_date_relation(date_time)
                date_info = format_date_info(date_time)
                specialist_name = result.get("doctor", "Специалист")

                # Check for error with no available slots
                if "status" in result and result["status"].startswith("error_empty_windows"):
                    status = f"error_empty_windows"
                    if date_relation == "today":
                        status = f"error_empty_windows_today"
                    elif date_relation == "tomorrow":
                        status = f"error_empty_windows_tomorrow"

                    response = {
                        "status": status,
                        "message": f"Свободных приемов {'на сегодня' if date_relation == 'today' else 'на завтра' if date_relation == 'tomorrow' else ''} не найдено."
                    }

                    if date_relation == "today":
                        response["day"] = "сегодня"
                        response["day_kz"] = "бүгін"
                    elif date_relation == "tomorrow":
                        response["day"] = "завтра"
                        response["day_kz"] = "ертең"

                    return response

                # Extract available times
                available_times = []

                if "all_available_times" in result and isinstance(result["all_available_times"], list):
                    available_times = result["all_available_times"]
                elif "time_1" in result and result["time_1"]:
                    for i in range(1, 4):
                        key = f"time_{i}"
                        if key in result and result[key]:
                            available_times.append(result[key])
                elif "first_time" in result and result["first_time"]:
                    for key in ["first_time", "second_time", "third_time"]:
                        if key in result and result[key]:
                            available_times.append(result[key])

                # Clean times (remove seconds, extract from datetime strings)
                clean_times = []
                for t in available_times:
                    if isinstance(t, str):
                        # Extract time part if in datetime format
                        if " " in t:
                            clean_times.append(t.split(" ")[1])
                        else:
                            # Remove seconds if present
                            if t.count(":") == 2:
                                clean_times.append(":".join(t.split(":")[:2]))
                            else:
                                clean_times.append(t)

                # Determine status based on number of times
                if len(clean_times) == 0:
                    status = "error_empty_windows"
                    if date_relation == "today":
                        status = "error_empty_windows_today"
                    elif date_relation == "tomorrow":
                        status = "error_empty_windows_tomorrow"

                    response = {
                        "status": status,
                        "message": f"Свободных приемов {'на сегодня' if date_relation == 'today' else 'на завтра' if date_relation == 'tomorrow' else ''} не найдено."
                    }
                elif len(clean_times) == 1:
                    status = "only_first_time"
                    if date_relation == "today":
                        status = "only_first_time_today"
                    elif date_relation == "tomorrow":
                        status = "only_first_time_tomorrow"

                    response = {
                        "status": status,
                        "date": date_info["date"],
                        "date_kz": date_info["date_kz"],
                        "specialist_name": specialist_name,
                        "weekday": date_info["weekday"],
                        "weekday_kz": date_info["weekday_kz"],
                        "first_time": clean_times[0]
                    }
                elif len(clean_times) == 2:
                    status = "only_two_time"
                    if date_relation == "today":
                        status = "only_two_time_today"
                    elif date_relation == "tomorrow":
                        status = "only_two_time_tomorrow"

                    response = {
                        "status": status,
                        "date": date_info["date"],
                        "date_kz": date_info["date_kz"],
                        "specialist_name": specialist_name,
                        "weekday": date_info["weekday"],
                        "weekday_kz": date_info["weekday_kz"],
                        "first_time": clean_times[0],
                        "second_time": clean_times[1]
                    }
                else:
                    status = "which_time"
                    if date_relation == "today":
                        status = "which_time_today"
                    elif date_relation == "tomorrow":
                        status = "which_time_tomorrow"

                    response = {
                        "status": status,
                        "date": date_info["date"],
                        "date_kz": date_info["date_kz"],
                        "specialist_name": specialist_name,
                        "weekday": date_info["weekday"],
                        "weekday_kz": date_info["weekday_kz"],
                        "first_time": clean_times[0],
                        "second_time": clean_times[1],
                        "third_time": clean_times[2]
                    }

                # Add day information for today/tomorrow
                if date_relation == "today":
                    response["day"] = "сегодня"
                    response["day_kz"] = "бүгін"
                elif date_relation == "tomorrow":
                    response["day"] = "завтра"
                    response["day_kz"] = "ертең"

                return response

            # For appointment_time_for_patient function
            elif function_name == "appointment_time_for_patient":
                if "status" in result and result["status"] == "error_no_appointment":
                    return {
                        "status": "error_reception_unavailable",
                        "message": "У пациента нет активных записей на прием"
                    }

                if "appointment_time" in result and "appointment_date" in result:
                    date_info = format_date_info(result["appointment_date"])
                    date_relation = get_date_relation(result["appointment_date"])

                    response = {
                        "status": "success_for_check_info",
                        "specialist_name": result.get("doctor_name", "Специалист"),
                        "date": date_info["date"],
                        "date_kz": date_info["date_kz"],
                        "weekday": date_info["weekday"],
                        "weekday_kz": date_info["weekday_kz"],
                        "time": result["appointment_time"]
                    }

                    if date_relation == "today":
                        response["day"] = "сегодня"
                        response["day_kz"] = "бүгін"
                    elif date_relation == "tomorrow":
                        response["day"] = "завтра"
                        response["day_kz"] = "ертең"

                    return response

                # If can't format properly, return original
                return result

            # For reserve_reception_for_patient function
            elif function_name == "reserve_reception_for_patient":
                date_from_patient = function_args.get("date_from_patient", "")
                date_relation = get_date_relation(date_from_patient)
                date_info = format_date_info(date_from_patient)

                status = result.get("status", "")

                # Success case
                if status == "success_schedule" or status.startswith("success_change_reception"):
                    success_status = "success_change_reception"
                    if date_relation == "today":
                        success_status = "success_change_reception_today"
                    elif date_relation == "tomorrow":
                        success_status = "success_change_reception_tomorrow"

                    # Clean time format
                    time_value = result.get("time", "")
                    if " " in time_value:
                        time_value = time_value.split(" ")[1]

                    # Remove seconds if present
                    if time_value.count(":") == 2:
                        time_value = ":".join(time_value.split(":")[:2])

                    response = {
                        "status": success_status,
                        "date": date_info["date"],
                        "date_kz": date_info["date_kz"],
                        "specialist_name": result.get("specialist_name", "Специалист"),
                        "weekday": date_info["weekday"],
                        "weekday_kz": date_info["weekday_kz"],
                        "time": time_value
                    }

                    if date_relation == "today":
                        response["day"] = "сегодня"
                        response["day_kz"] = "бүгін"
                    elif date_relation == "tomorrow":
                        response["day"] = "завтра"
                        response["day_kz"] = "ертең"

                    return response

                # Error case with alternative times
                elif status == "suggest_times" and "suggested_times" in result:
                    suggested_times = result["suggested_times"]

                    # Clean times
                    clean_times = []
                    for t in suggested_times:
                        if " " in t:
                            clean_times.append(t.split(" ")[1])
                        else:
                            clean_times.append(t)

                    # Determine status based on number of times
                    if len(clean_times) == 0:
                        error_status = "error_empty_windows"
                        if date_relation == "today":
                            error_status = "error_empty_windows_today"
                        elif date_relation == "tomorrow":
                            error_status = "error_empty_windows_tomorrow"

                        response = {
                            "status": error_status,
                            "message": f"Свободных приемов не найдено."
                        }
                    elif len(clean_times) == 1:
                        error_status = "change_only_first_time"
                        if date_relation == "today":
                            error_status = "change_only_first_time_today"
                        elif date_relation == "tomorrow":
                            error_status = "change_only_first_time_tomorrow"

                        response = {
                            "status": error_status,
                            "date": date_info["date"],
                            "date_kz": date_info["date_kz"],
                            "specialist_name": result.get("specialist_name", "Специалист"),
                            "weekday": date_info["weekday"],
                            "weekday_kz": date_info["weekday_kz"],
                            "first_time": clean_times[0]
                        }
                    elif len(clean_times) == 2:
                        error_status = "change_only_two_time"
                        if date_relation == "today":
                            error_status = "change_only_two_time_today"
                        elif date_relation == "tomorrow":
                            error_status = "change_only_two_time_tomorrow"

                        response = {
                            "status": error_status,
                            "date": date_info["date"],
                            "date_kz": date_info["date_kz"],
                            "specialist_name": result.get("specialist_name", "Специалист"),
                            "weekday": date_info["weekday"],
                            "weekday_kz": date_info["weekday_kz"],
                            "first_time": clean_times[0],
                            "second_time": clean_times[1]
                        }
                    else:
                        error_status = "error_change_reception"
                        if date_relation == "today":
                            error_status = "error_change_reception_today"
                        elif date_relation == "tomorrow":
                            error_status = "error_change_reception_tomorrow"

                        response = {
                            "status": error_status,
                            "date": date_info["date"],
                            "date_kz": date_info["date_kz"],
                            "specialist_name": result.get("specialist_name", "Специалист"),
                            "weekday": date_info["weekday"],
                            "weekday_kz": date_info["weekday_kz"],
                            "first_time": clean_times[0],
                            "second_time": clean_times[1],
                            "third_time": clean_times[2] if len(clean_times) > 2 else None
                        }

                    # Add day information
                    if date_relation == "today":
                        response["day"] = "сегодня"
                        response["day_kz"] = "бүгін"
                    elif date_relation == "tomorrow":
                        response["day"] = "завтра"
                        response["day_kz"] = "ертең"

                    # Remove None values
                    response = {k: v for k, v in response.items() if v is not None}

                    return response

                # Bad date error
                elif status == "error_change_reception_bad_date" or "date" in result.get("message", "").lower():
                    return {
                        "status": "error_change_reception_bad_date",
                        "data": result.get("message", "Неверный формат даты")
                    }

                # Non-working time
                elif status == "nonworktime":
                    return {"status": "nonworktime"}

                # If can't format properly, return original
                return result

            # For delete_reception_for_patient function
            elif function_name == "delete_reception_for_patient":
                status = result.get("status", "")

                if status == "success_delete":
                    return {
                        "status": "success_deleting_reception",
                        "message": "Запись успешно удалена"
                    }
                else:
                    return {
                        "status": "error_deleting_reception",
                        "message": result.get("message", "Ошибка при удалении записи")
                    }

            # Unknown function - return original
            return result

        except Exception as e:
            logger.error(f"Error formatting response for ACS: {e}", exc_info=True)
            return {
                "status": "error",
                "message": f"Ошибка форматирования ответа: {str(e)}"
            }

    def _call_function(self, function_name: str, function_args: dict, thread_id: str = None) -> dict:
        """
        Calls the appropriate function with given arguments

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

            # Function mapping
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

                # If result is JsonResponse, convert to dict
                if hasattr(result, 'content'):
                    return json.loads(result.content.decode('utf-8'))
                return result

            elif function_name == "appointment_time_for_patient":
                patient_code = function_args.get("patient_code")
                year_from_patient = function_args.get("year_from_patient_for_returning")

                # Call function
                result = appointment_time_for_patient(patient_code, year_from_patient)

                # If result is JsonResponse, convert to dict
                if hasattr(result, 'content'):
                    return json.loads(result.content.decode('utf-8'))
                return result

            elif function_name == "reserve_reception_for_patient":
                patient_id = function_args.get("patient_id")
                date_from_patient = function_args.get("date_from_patient")
                trigger_id = function_args.get("trigger_id", 1)

                # Handle special date values for function
                if date_from_patient:
                    # Extract date part if only date is provided (add default time)
                    if ' ' not in date_from_patient:
                        date_from_patient = f"{date_from_patient} 10:00"

                    # Look for time of day mentions
                    user_message = self._get_last_user_message(thread_id) if thread_id else ""
                    time_periods = [
                        ("утр", "09:00"), ("утром", "09:00"), ("рано", "09:00"),
                        ("обед", "13:00"), ("днем", "13:00"), ("полдень", "13:00"),
                        ("вечер", "18:00"), ("поздн", "18:00"), ("веч", "18:00")
                    ]

                    # Check if a specific time period was mentioned
                    for keyword, default_time in time_periods:
                        if keyword in user_message.lower():
                            date_part = date_from_patient.split(' ')[0]
                            date_from_patient = f"{date_part} {default_time}"
                            break

                # Call function
                result = reserve_reception_for_patient(patient_id, date_from_patient, trigger_id)

                # If result is JsonResponse, convert to dict
                if hasattr(result, 'content'):
                    return json.loads(result.content.decode('utf-8'))
                return result

            elif function_name == "delete_reception_for_patient":
                patient_id = function_args.get("patient_id")

                # Call function
                result = delete_reception_for_patient(patient_id)

                # If result is JsonResponse, convert to dict
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
        Gets the last user message from a thread

        Args:
            thread_id: Thread ID

        Returns:
            str: Last user message content
        """
        try:
            messages = self.client.beta.threads.messages.list(
                thread_id=thread_id,
                limit=10,
                order="desc"
            )

            for message in messages.data:
                if message.role == "user":
                    if message.content and len(message.content) > 0:
                        return message.content[0].text.value

            return ""
        except Exception as e:
            logger.error(f"Error getting last user message: {e}")
            return ""

    def get_messages(self, thread_id: str, limit: int = 10) -> List[dict]:
        """
        Получает сообщения из треда.

        Args:
            thread_id: ID треда
            limit: Максимальное количество сообщений

        Returns:
            List[dict]: Список сообщений
        """
        try:
            messages = self.client.beta.threads.messages.list(
                thread_id=thread_id,
                limit=limit,
                order="desc"  # Получаем новые сообщения первыми
            )
            logger.info(f"Получено {len(messages.data)} сообщений из треда {thread_id}")
            return messages.data
        except Exception as e:
            logger.error(f"Ошибка при получении сообщений: {str(e)}")
            raise

    def wait_for_run_completion(self, thread_id: str, run_id: str, timeout: int = 60) -> dict:
        """
        Waits for completion of an assistant run, handling function calls.
        IMPORTANT: Now immediately returns formatted function results without waiting for text response.

        Args:
            thread_id: Thread ID
            run_id: Run ID
            timeout: Maximum wait time in seconds

        Returns:
            dict: Formatted function results or status
        """
        import time

        logger.info(f"Waiting for completion of run {run_id} (timeout: {timeout}s)")

        start_time = time.time()
        function_results = None

        try:
            while time.time() - start_time < timeout:
                run = self.client.beta.threads.runs.retrieve(
                    thread_id=thread_id,
                    run_id=run_id
                )

                current_status = run.status
                self._update_run_status(run_id, current_status)

                # Handle function calls if needed
                if current_status == "requires_action":
                    logger.info(f"Run {run_id} requires action")
                    function_results = self.handle_function_calls(run_id, thread_id)
                    logger.info(f"Function call result: {function_results}")

                    # IMPORTANT FIX: Immediately return function results without waiting for completion
                    if function_results:
                        logger.info(f"Immediately returning function results: {function_results}")
                        # Cancel the run to avoid further processing
                        try:
                            self._cancel_run(thread_id, run_id)
                        except Exception as e:
                            logger.warning(f"Could not cancel run, but continuing: {e}")
                        return function_results

                    continue

                # Return if the run has completed
                if current_status in ["completed", "failed", "cancelled", "expired"]:
                    logger.info(f"Run {run_id} ended with status: {current_status}")
                    return function_results if function_results else {"status": current_status}

                # Wait before polling again
                time.sleep(1.5)

            # If we reach here, we exceeded the timeout
            logger.warning(f"Run {run_id} exceeded timeout {timeout}s, cancelling")
            self._cancel_run(thread_id, run_id)
            return {"status": "cancelled"}

        except Exception as e:
            logger.error(f"Error in wait_for_run_completion: {str(e)}")
            return {"status": "error", "message": str(e)}

    def _check_run_status(self, thread_id: str, run_id: str) -> str:
        """
        Checks the current status of a run.

        Args:
            thread_id: Thread ID
            run_id: Run ID

        Returns:
            str: Current run status
        """
        try:
            run = self.client.beta.threads.runs.retrieve(
                thread_id=thread_id,
                run_id=run_id
            )
            status = run.status
            self._update_run_status(run_id, status)
            return status
        except Exception as e:
            logger.error(f"Error checking run status: {str(e)}")
            return "unknown"

    def _update_run_status(self, run_id: str, status: str) -> None:
        """
        Helper method to update the run status in the database.

        Args:
            run_id: Run ID
            status: New status
        """
        run_model = RunModel.objects.filter(run_id=run_id).first()
        if run_model and run_model.status != status:
            run_model.status = status
            run_model.save()
            logger.info(f"Updated run status to: {status}")

    def _poll_run_completion(self, thread_id: str, run_id: str, timeout: int = 60) -> str:
        """
        Fallback method that polls for run completion if streaming fails.

        Args:
            thread_id: Thread ID
            run_id: Run ID
            timeout: Timeout in seconds

        Returns:
            str: Final run status
        """
        import time

        start_time = time.time()
        poll_interval = 1.0  # Start with a 1 second interval

        logger.info(f"Polling for completion of run {run_id} (timeout: {timeout}s)")

        while time.time() - start_time < timeout:
            try:
                run = self.client.beta.threads.runs.retrieve(
                    thread_id=thread_id,
                    run_id=run_id
                )

                current_status = run.status
                self._update_run_status(run_id, current_status)

                # Handle function calls if needed
                if current_status == "requires_action":
                    logger.info(f"Run {run_id} requires action")
                    result = self.handle_function_calls(run_id, thread_id)
                    logger.info(f"Function call result: {result}")
                    # Reset the poll interval after handling function calls
                    poll_interval = 1.0
                    continue

                # Return if the run has completed
                if current_status in ["completed", "failed", "cancelled", "expired"]:
                    logger.info(f"Run {run_id} ended with status: {current_status}")
                    return current_status

                # Use exponential backoff for polling (up to 5 seconds)
                poll_interval = min(5.0, poll_interval * 1.5)
                time.sleep(poll_interval)

            except Exception as e:
                logger.error(f"Error polling run status: {str(e)}")
                time.sleep(poll_interval)

        # If we reach here, we exceeded the timeout
        logger.warning(f"Run {run_id} exceeded timeout {timeout}s, cancelling")
        self._cancel_run(thread_id, run_id)
        return "cancelled"

    def _cancel_run(self, thread_id: str, run_id: str) -> None:
        """
        Helper method to cancel a run and update its status in the database.
        Enhanced with better error handling for already canceled runs.

        Args:
            thread_id: Thread ID
            run_id: Run ID
        """
        try:
            # First check the current run status to avoid trying to cancel an already canceled run
            run = self.client.beta.threads.runs.retrieve(
                thread_id=thread_id,
                run_id=run_id
            )

            # Only attempt to cancel if the run is not already in a terminal state
            if run.status not in ["completed", "failed", "cancelled", "expired"]:
                self.client.beta.threads.runs.cancel(
                    thread_id=thread_id,
                    run_id=run_id
                )
                logger.info(f"Successfully cancelled run {run_id}")
            else:
                logger.info(f"Run {run_id} already in terminal state: {run.status}, no need to cancel")

            # Update status in database regardless
            self._update_run_status(run_id, "cancelled")
        except Exception as e:
            # Check for the specific error about already cancelled runs
            error_message = str(e)
            if "Cannot cancel run with status 'cancelled'" in error_message:
                logger.info(f"Run {run_id} was already cancelled")
                self._update_run_status(run_id, "cancelled")
            else:
                logger.error(f"Error cancelling run: {e}")

    def _extract_time_of_day(self, text: str) -> Optional[str]:
        """
        Извлекает указания на время суток из текста.

        Args:
            text: Текст запроса пользователя

        Returns:
            Optional[str]: Время суток или None
        """
        if any(kw in text for kw in ["утр", "утром", "с утра", "на утро", "рано"]):
            return "утро"
        elif any(kw in text for kw in ["обед", "днем", "дневн", "полдень"]):
            return "обед"
        elif any(kw in text for kw in ["вечер", "ужин", "вечером", "поздн"]):
            return "вечер"
        return None

    def _parse_relative_date(self, text: str) -> str:
        """
        Парсит относительные даты типа 'через неделю'.

        Args:
            text: Текст с относительной датой

        Returns:
            str: Строка с конкретной датой
        """
        today = datetime.now()
        text = text.lower()

        # Извлекаем количество дней/недель/месяцев
        match = re.search(r'через (\d+) (день|дня|дней|недел[юяи]|месяц|месяца|месяцев)', text)
        if match:
            number = int(match.group(1))
            unit = match.group(2)

            if "день" in unit or "дня" in unit or "дней" in unit:
                target_date = today + timedelta(days=number)
            elif "недел" in unit:
                target_date = today + timedelta(weeks=number)
            elif "месяц" in unit or "месяца" in unit or "месяцев" in unit:
                # Приблизительно месяц как 30 дней
                target_date = today + timedelta(days=number * 30)

            return target_date.strftime("%Y-%m-%d")

        # Обработка "через неделю" без числа
        if "через неделю" in text:
            target_date = today + timedelta(weeks=1)
            return target_date.strftime("%Y-%m-%d")

        return text  # Возвращаем оригинал, если нет совпадений

    def _get_last_user_message(self, thread_id: str) -> str:
        """
        Получает последнее сообщение пользователя из треда.

        Args:
            thread_id: ID треда

        Returns:
            str: Текст последнего сообщения пользователя
        """
        try:
            messages = self.client.beta.threads.messages.list(
                thread_id=thread_id,
                limit=10,
                order="desc"
            )

            for message in messages.data:
                if message.role == "user":
                    # Получаем текст сообщения
                    if message.content and len(message.content) > 0:
                        return message.content[0].text.value

            return ""
        except Exception as e:
            logger.error(f"Ошибка получения последнего сообщения пользователя: {str(e)}")
            return ""

    def _round_to_nearest_half_hour(self, time_str):
        """
        Rounds a time string to the nearest half-hour since the clinic operates
        on 30-minute intervals.

        Args:
            time_str: Time string in format "HH:MM"

        Returns:
            Time string rounded to nearest half-hour
        """
        try:
            hour, minute = map(int, time_str.split(':'))

            # Round minutes to nearest 30
            if minute < 15:
                # Round down to the hour
                new_minute = 0
            elif 15 <= minute < 45:
                # Round to half hour
                new_minute = 30
            else:
                # Round up to the next hour
                new_minute = 0
                hour += 1

            # Handle hour overflow
            if hour >= 24:
                hour = 0

            return f"{hour:02d}:{new_minute:02d}"
        except Exception as e:
            logger.error(f"Error rounding time '{time_str}': {e}")
            # Return original if parsing fails
            return time_str

    def get_last_function_call_result(self, thread_id: str, run_id: str) -> dict:
        """
        Gets the result of the last function call from a run.

        Args:
            thread_id: Thread ID
            run_id: Run ID

        Returns:
            dict: Function call result or empty dict if none found
        """
        try:
            run = self.client.beta.threads.runs.retrieve(
                thread_id=thread_id,
                run_id=run_id
            )

            # Check if run has tool calls
            if run.status == "requires_action" and run.required_action:
                tool_calls = run.required_action.submit_tool_outputs.tool_calls
                for tool_call in tool_calls:
                    try:
                        function_name = tool_call.function.name
                        function_args = json.loads(tool_call.function.arguments)

                        # Call function and get result
                        result = self._call_function(function_name, function_args, thread_id)

                        # Format result for ACS
                        formatted_result = self._format_for_acs(function_name, function_args, result)

                        return formatted_result
                    except Exception as e:
                        logger.error(f"Error getting function call result: {e}")

            return {}
        except Exception as e:
            logger.error(f"Error retrieving run: {e}")
            return {}
