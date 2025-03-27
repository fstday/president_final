import os
import re
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Union
from django.utils import timezone
from django.conf import settings
from openai import OpenAI

from reminder.models import Assistant, Thread, Run as RunModel, Patient, Appointment, QueueInfo
from reminder.openai_assistant.assistant_instructions import get_enhanced_assistant_prompt

logger = logging.getLogger(__name__)

# Определение карты соответствий времени суток к конкретным часам
time_mappings = {
    # Утро
    "утро": "09:00",
    "утром": "09:00",
    "с утра": "09:00",
    "на утро": "09:00",
    "пораньше": "09:00",
    "рано": "09:00",
    "раннее": "09:00",

    # Обед
    "обед": "13:00",
    "на обед": "13:00",
    "в обед": "13:00",
    "полдень": "12:00",
    "в полдень": "12:00",
    "дневное": "13:00",
    "днем": "13:00",

    # После обеда
    "после обеда": "15:00",
    "послеобеденное": "15:00",
    "дневное время": "15:00",

    # До обеда
    "до обеда": "11:00",
    "перед обедом": "11:00",
    "предобеденное": "11:00",

    # Вечер
    "вечер": "18:00",
    "вечером": "18:00",
    "на вечер": "18:00",
    "ужин": "18:00",
    "на ужин": "18:00",
    "к ужину": "18:00",
    "поздно": "19:00",
    "попозже": "19:00",
    "позднее": "19:00"
}


class AssistantClient:
    """
    Client for working with OpenAI Assistant API.
    Provides interaction between patients and the appointment system.
    """

    def __init__(self):
        self.client = OpenAI(api_key=settings.OPENAI_API_KEY)

    def get_or_create_thread(self, appointment_id: int) -> Thread:
        """
        Gets existing thread or creates a new one for patient dialogue
        """
        try:
            # Check if appointment exists
            appointment = Appointment.objects.get(appointment_id=appointment_id)

            # Find an active thread for this appointment
            thread = Thread.objects.filter(
                order_key=str(appointment_id),
                expires_at__gt=timezone.now()
            ).first()

            if thread:
                logger.info(f"Found existing thread {thread.thread_id} for appointment {appointment_id}")
                return thread

            # If no active thread exists, create a new one
            assistant = Assistant.objects.first()  # Get first assistant from DB
            if not assistant:
                logger.error("No assistants found in database")
                raise ValueError("No assistants found in database")

            # Create thread in OpenAI
            openai_thread = self.client.beta.threads.create()

            # Save thread in local database
            thread = Thread.objects.create(
                thread_id=openai_thread.id,
                order_key=str(appointment_id),
                assistant=assistant
            )
            logger.info(f"Created new thread {thread.thread_id} for appointment {appointment_id}")
            return thread

        except Appointment.DoesNotExist:
            logger.error(f"Appointment with ID {appointment_id} not found")
            raise ValueError(f"Appointment with ID {appointment_id} not found")
        except Exception as e:
            logger.error(f"Error creating/finding thread: {e}")
            raise

    def add_message_to_thread(self, thread_id: str, content: str, role: str = "user") -> dict:
        """
        Adds a message to the thread
        """
        try:
            message = self.client.beta.threads.messages.create(
                thread_id=thread_id,
                role=role,
                content=content
            )
            logger.info(f"Added {role} message to thread {thread_id}")
            return message
        except Exception as e:
            logger.error(f"Error adding message to thread: {e}")
            raise

    def run_assistant(self, thread: Thread, appointment: Appointment) -> RunModel:
        """
        Runs assistant to process messages in the thread
        """
        try:
            # Get patient and appointment data for context
            patient = appointment.patient
            patient_code = patient.patient_code

            # Format the appointment time
            appointment_time_str = "Не указано"
            if appointment.start_time:
                appointment_time_str = appointment.start_time.strftime("%Y-%m-%d %H:%M")

            # Get the doctor's name
            doctor_name = "Не указан"
            if appointment.doctor:
                doctor_name = appointment.doctor.full_name

            # Get the clinic name
            clinic_name = "Не указана"
            if appointment.clinic:
                clinic_name = appointment.clinic.name

            current_date = datetime.now().strftime("%Y-%m-%d")
            tomorrow_date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

            enhanced_instructions = get_enhanced_assistant_prompt()
            context_instructions = enhanced_instructions + f"""
                # КОНТЕКСТ ТЕКУЩЕГО РАЗГОВОРА
            # МЕДИЦИНСКИЙ АССИСТЕНТ ДЛЯ УПРАВЛЕНИЯ ЗАПИСЯМИ НА ПРИЕМ

            ## ОСНОВНАЯ ЗАДАЧА
            Ты AI-ассистент для системы управления медицинскими записями, интегрированной с Infoclinica и голосовым роботом ACS. Твоя главная цель - анализировать запросы пациентов на естественном языке, определять нужное действие, ВЫЗЫВАТЬ СООТВЕТСТВУЮЩУЮ ФУНКЦИЮ и форматировать ответ по требованиям системы.

            ## КРИТИЧЕСКИ ВАЖНЫЕ ПРАВИЛА

            ### Обязательное использование функций
            1. Свободные окошки → which_time_in_certain_day(reception_id, date_time)
            2. Текущая запись → appointment_time_for_patient(patient_code)
            3. Запись/Перенос → reserve_reception_for_patient(patient_id, date_from_patient, trigger_id)
            4. Отмена записи → delete_reception_for_patient(patient_id)

            ## ДЕТАЛЬНЫЕ ПРАВИЛА ИНТЕРПРЕТАЦИИ ЗАПРОСОВ

            ### 1. Выбор времени при переносе записи

            #### Стратегии оптимального выбора времени:
            - Ближайшее свободное время → выбирать время, доступное раньше всех
            - Удобство после обеда → предпочтительно время после 13:00
            - Меньше людей → время близкое к началу (09:00) или концу (20:00) рабочего дня

            #### Специфические сценарии переноса:
            - Перенос "на раньше" → только время до текущей записи, в тот же день
            - Перенос "на позже" → только время после текущей записи, в тот же день
            - Перенос без уточнения дня → время в день текущей записи
            - Перенос "на вечер" → время после 16:00, если доступно

            ### 2. Особенности обработки дат и времени

            #### Временные соответствия:
            - "Утро" → 09:00-11:00
            - "День", "Обед" → 12:00-15:00
            - "Вечер" → 16:00-20:00
            - "Раньше" → минимум на 30-60 минут раньше текущего времени
            - "Позже" → минимум на 30-60 минут позже текущего времени

            #### Относительные даты:
            - "Перенести на послезавтра" → действие 'reserve', дата через 2 дня от текущей
            - "Перенести на сегодня" → действие 'reserve', сегодняшняя дата
            - "Перенести на завтра" → действие 'reserve', завтрашняя дата

            ### 3. Правила удаления записи

            #### Слова для удаления:
            ✅ Разрешенные: 
            - "удалить", "удалите"
            - "отменить", "отмените"
            - "убрать"
            - "отказаться"
            - "не хочу"
            - "перестаньте"
            - "уберите запись"
            - "исключить"
            - "закрыть"
            - "отказ"
            - "не актуально"
            - "больше не нужно"
            - "не требуется"

            ❌ НЕ считать удалением: 
            - "перенеси"
            - "перенесите"
            - "переоформите"
            - "запишите"
            - "записать"

            ### 4. Обработка неоднозначных запросов

            #### Короткие или неопределенные запросы:
            - Возвращать `bad_user_input`
            - Исключения: фразы с "раньше" или "позже"

            #### Обработка времени с неточными интервалами:
            - 00-15 минут → округление вниз
            - 16-45 минут → округление до 30 минут
            - 46-59 минут → округление вверх

            ### 5. Особые сценарии

            #### При переносе "позже" или "попозже":
            - Не удалять текущую запись, если не удалось найти новое время
            - Предлагать альтернативные варианты

            ### 6. Выбор времени из предложенных вариантов

            #### Если предложены времена, например: ['10:00', '10:30', '11:00']

            ##### Первый вариант (индекс 0):
            - "Давайте 1 вариант"
            - "Первый вариант"
            - "Запишите на первое время"
            - Действие: 'reserve', время: 10:00

            ##### Второй вариант (индекс 1):
            - "Давайте 2 вариант"
            - "Второй вариант"
            - "Запишите на второе время"
            - Действие: 'reserve', время: 10:30

            ##### Третий/последний вариант (индекс -1):
            - "Давайте 3 вариант"
            - "Последнее время"
            - "Запишите на третье время"
            - Действие: 'reserve', время: 11:00

            ## КОНТЕКСТ ТЕКУЩЕГО РАЗГОВОРА
            - Пациент: {patient.full_name} (ID: {patient_code})
            - Текущая запись: {appointment.appointment_id} на {appointment_time_str}
            - Врач: {doctor_name}
            - Клиника: {clinic_name}
            
            ## ОБРАБОТКА ДАТ И ВРЕМЕНИ
            - Сегодняшняя дата: {current_date}
            - Завтрашняя дата: {tomorrow_date}
            - Когда пользователь говорит "сегодня", используй дату {current_date}
            - Когда пользователь говорит "завтра", используй дату {tomorrow_date}

            
            ## ФИНАЛЬНЫЕ ИНСТРУКЦИИ
            ✔️ ВСЕГДА использовать функции вместо текстовых ответов
            ✔️ Точно определять намерение пользователя
            ✔️ Учитывать контекст текущей записи
            ✔️ При невозможности выполнить действие - предлагать альтернативы
            
            ## ДЕТАЛЬНАЯ КАРТА ВРЕМЕНИ
            
            Используй эту карту соответствий для определения конкретного времени при запросах пациентов:
            
            ### Утро (09:00-11:00):
            - "утро", "утром", "с утра", "на утро" → 09:00
            - "пораньше", "рано", "раннее" → 09:00
            
            ### Обед (12:00-14:00):
            - "обед", "на обед", "в обед" → 13:00
            - "полдень", "в полдень" → 12:00
            - "дневное", "днем" → 13:00
            
            ### До и после обеда:
            - "после обеда", "послеобеденное", "дневное время" → 15:00
            - "до обеда", "перед обедом", "предобеденное" → 11:00
            
            ### Вечер (17:00-20:00):
            - "вечер", "вечером", "на вечер" → 18:00
            - "ужин", "на ужин", "к ужину" → 18:00
            - "поздно", "попозже", "позднее" → 19:00
            
            ВАЖНО: Когда пациент запрашивает запись на определенное время суток (утро, день, вечер) - 
            ВСЕГДА вызывай функцию reserve_reception_for_patient, используя наиболее подходящее время 
            из карты соответствий. НИКОГДА не отвечай текстом.
            """

            # Проверяем, какие функции зарегистрированы у ассистента
            try:
                assistant_info = self.client.beta.assistants.retrieve(thread.assistant.assistant_id)
                logger.info(f"🔍 Зарегистрированные функции у ассистента: {assistant_info.tools}")
            except Exception as e:
                logger.error(f"❌ Ошибка при получении списка функций ассистента: {e}")

            # Create an assistant run with additional context
            openai_run = self.client.beta.threads.runs.create(
                thread_id=thread.thread_id,
                assistant_id=thread.assistant.assistant_id,
                instructions=context_instructions
            )

            # Save run information in DB
            run = RunModel.objects.create(
                run_id=openai_run.id,
                status=openai_run.status
            )

            # Update thread with current run
            thread.current_run = run
            thread.save()

            logger.info(f"Started run {run.run_id} for thread {thread.thread_id}")
            return run
        except Exception as e:
            logger.error(f"Error running assistant: {e}", exc_info=True)
            raise

    def handle_function_calls(self, run_id: str, thread_id: str):
        """
        Handles function calls from the assistant
        """
        try:
            run = self.client.beta.threads.runs.retrieve(
                thread_id=thread_id,
                run_id=run_id
            )

            # Если action required
            if run.status == "requires_action" and run.required_action:
                tool_calls = run.required_action.submit_tool_outputs.tool_calls
                logger.info(f"Function calls detected: {len(tool_calls)}")

                tool_outputs = []
                for tool_call in tool_calls:
                    function_name = tool_call.function.name
                    function_args = json.loads(tool_call.function.arguments)

                    logger.info(f"Function call detected: {function_name} with args: {function_args}")

                    # Get result of function call
                    result = self._call_function(function_name, function_args, thread_id)
                    logger.info(f"Function result: {result}")

                    tool_outputs.append({
                        "tool_call_id": tool_call.id,
                        "output": json.dumps(result)
                    })

                # Submit function call results
                if tool_outputs:
                    self.client.beta.threads.runs.submit_tool_outputs(
                        thread_id=thread_id,
                        run_id=run_id,
                        tool_outputs=tool_outputs
                    )
                    logger.info(f"Submitted {len(tool_outputs)} tool outputs for run {run_id}")
                    return tool_outputs
                else:
                    logger.warning("No tool outputs to submit")
                    return []

            return run.status

        except Exception as e:
            logger.error(f"Error handling function calls: {e}", exc_info=True)
            raise

    def _call_function(self, function_name: str, function_args: dict, thread_id: str = None) -> dict:
        """
        Enhanced function to properly handle appointment scheduling according to the required algorithm
        """
        # Import functions
        from reminder.infoclinica_requests.schedule.delete_reception_for_patient import delete_reception_for_patient
        from reminder.infoclinica_requests.schedule.reserve_reception_for_patient import reserve_reception_for_patient
        from reminder.infoclinica_requests.schedule.appointment_time_for_patient import appointment_time_for_patient
        from reminder.infoclinica_requests.schedule.which_time_in_certain_day import which_time_in_certain_day
        from datetime import datetime, timedelta

        try:
            logger.info(f"Calling function {function_name} with args: {function_args}")

            # Process relative date references
            if function_name == "reserve_reception_for_patient":
                date_from_patient = function_args.get("date_from_patient", "")
                patient_id = function_args.get("patient_id")

                # Handle relative date references
                if isinstance(date_from_patient, str) and "через" in date_from_patient.lower():
                    # Parse "через неделю", "через 3 дня", etc.
                    relative_date = self._parse_relative_date(date_from_patient)
                    if relative_date:
                        function_args["date_from_patient"] = relative_date

            # CASE 1: Delete appointment
            if function_name == "delete_reception_for_patient":
                patient_id = function_args.get("patient_id")
                logger.info(f"Deleting reception for patient {patient_id}")
                return delete_reception_for_patient(patient_id)

            # CASE 2: Scheduling appointment
            elif function_name == "reserve_reception_for_patient":
                patient_id = function_args.get("patient_id")
                date_from_patient = function_args.get("date_from_patient")
                trigger_id = function_args.get("trigger_id", 1)

                # Handle time-of-day references
                if isinstance(date_from_patient, str) and not re.search(r'\d{1,2}:\d{2}', date_from_patient):
                    # Check for time of day references
                    date_part = date_from_patient.split()[0] if ' ' in date_from_patient else date_from_patient
                    time_of_day = self._extract_time_of_day(date_from_patient.lower())
                    if time_of_day:
                        # Map time of day to specific hour
                        if "утр" in time_of_day:
                            date_from_patient = f"{date_part} 10:30"
                        elif "обед" in time_of_day:
                            date_from_patient = f"{date_part} 13:30"
                        elif "вечер" in time_of_day or "ужин" in time_of_day:
                            date_from_patient = f"{date_part} 18:30"
                        function_args["date_from_patient"] = date_from_patient

                return reserve_reception_for_patient(patient_id, date_from_patient, trigger_id)

            # CASE 3: Get appointment information
            elif function_name == "appointment_time_for_patient":
                patient_code = function_args.get("patient_code")
                year_from_patient_for_returning = function_args.get("year_from_patient_for_returning")
                logger.info(f"Getting appointment time for patient {patient_code}")
                return appointment_time_for_patient(patient_code, year_from_patient_for_returning)

            elif function_name == "which_time_in_certain_day":

                patient_code = function_args.get("patient_code")

                date_time = function_args.get("date_time")

                # Получаем последнее сообщение пользователя

                user_request = self._get_last_user_message(thread_id)

                logger.info(f"User request for which_time_in_certain_day: {user_request}")

                # КРИТИЧЕСКИ ВАЖНО: Проверяем, содержит ли запрос ключевые слова для записи

                scheduling_keywords = [

                    "запиши", "запишите", "записать", "записаться",

                    "назначь", "назначьте", "оформи", "оформите",

                    "хочу на", "хочу записаться", "хочу запись",

                    "сделай", "сделайте", "забронируй", "бронь"

                ]

                is_scheduling_request = any(keyword in user_request.lower() for keyword in scheduling_keywords)

                logger.info(f"Is scheduling request: {is_scheduling_request}")

                # Получаем доступные времена

                available_times_result = which_time_in_certain_day(patient_code, date_time)

                # Преобразуем JsonResponse в словарь при необходимости

                if hasattr(available_times_result, 'content'):
                    available_times_result = json.loads(available_times_result.content.decode('utf-8'))

                # КРИТИЧЕСКОЕ УСЛОВИЕ: Если запрос на запись - мы ВСЕГДА должны завершить процесс записи

                if is_scheduling_request:

                    # Извлекаем список доступных времен, вне зависимости от структуры ответа

                    available_times = []

                    # Проверяем все возможные форматы ответа

                    if isinstance(available_times_result, dict):

                        # Вариант 1: all_available_times

                        if "all_available_times" in available_times_result:

                            available_times = available_times_result.get("all_available_times", [])


                        # Вариант 2: time_1, time_2, time_3

                        elif any(f"time_{i}" in available_times_result for i in range(1, 4)):

                            times = []

                            for i in range(1, 10):  # До 10 возможных времен

                                time_key = f"time_{i}"

                                if time_key in available_times_result and available_times_result[time_key]:
                                    times.append(available_times_result[time_key])

                            available_times = times

                    # Если нашли доступные времена - ОБЯЗАТЕЛЬНО выбираем одно и записываем

                    if available_times:

                        # По умолчанию выбираем первое доступное время

                        selected_time = available_times[0]

                        try:

                            # Пытаемся использовать продвинутую логику выбора

                            selected_time_complex = self.handle_time_selection(

                                available_times,

                                user_request,

                                patient_code

                            )

                            if selected_time_complex:
                                selected_time = selected_time_complex

                        except Exception as e:

                            logger.error(f"Error in handle_time_selection: {e}, falling back to first available time")

                        # Форматируем время для записи

                        formatted_datetime = selected_time

                        # Если время без даты - добавляем дату

                        if ' ' not in selected_time and ':' in selected_time:
                            formatted_datetime = f"{date_time} {selected_time}"

                        logger.info(f"Auto-scheduling appointment: {formatted_datetime}")

                        # КЛЮЧЕВОЙ ШАГ: Выполняем запись

                        reservation_result = reserve_reception_for_patient(

                            patient_id=patient_code,

                            date_from_patient=formatted_datetime,

                            trigger_id=1

                        )

                        # Возвращаем результат записи

                        return reservation_result

                # Если это не запрос на запись или нет доступных времен - возвращаем обычный результат

                return available_times_result

            else:
                logger.warning(f"Unknown function: {function_name}")
                return {"status": "error", "message": f"Unknown function: {function_name}"}

        except Exception as e:
            logger.error(f"Error calling function {function_name}: {e}", exc_info=True)
            return {"status": "error", "message": str(e)}

    def get_messages(self, thread_id: str, limit: int = 10) -> List[dict]:
        """
        Gets messages from thread
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
            logger.error(f"Error getting messages: {e}")
            raise

    def wait_for_run_completion(self, thread_id: str, run_id: str, timeout: int = 60) -> str:
        """
        Waits for assistant run to complete with periodic status checks
        """
        import time

        start_time = time.time()
        poll_interval = 1.0  # Start with 1 second polling

        logger.info(f"Waiting for run {run_id} to complete (timeout: {timeout}s)")

        while time.time() - start_time < timeout:
            try:
                run = self.client.beta.threads.runs.retrieve(
                    thread_id=thread_id,
                    run_id=run_id
                )

                # Handle function calls if needed
                if run.status == "requires_action":
                    logger.info(f"Run {run_id} requires action")
                    result = self.handle_function_calls(run_id, thread_id)
                    logger.info(f"Function call result: {result}")

                # Update status in DB
                run_model = RunModel.objects.filter(run_id=run_id).first()
                if run_model and run_model.status != run.status:
                    run_model.status = run.status
                    run_model.save()
                    logger.info(f"Updated run status to {run.status}")

                # Check if run is complete
                if run.status in ["completed", "failed", "cancelled", "expired"]:
                    logger.info(f"Run {run_id} finished with status: {run.status}")
                    return run.status

                # Use exponential backoff for polling (up to 5 seconds)
                poll_interval = min(5.0, poll_interval * 1.5)
                time.sleep(poll_interval)

            except Exception as e:
                logger.error(f"Error checking run status: {e}")
                time.sleep(poll_interval)

        # If timeout, cancel the run
        logger.warning(f"Run {run_id} timed out after {timeout}s, cancelling")
        try:
            self.client.beta.threads.runs.cancel(
                thread_id=thread_id,
                run_id=run_id
            )

            # Update status in DB
            run_model = RunModel.objects.filter(run_id=run_id).first()
            if run_model:
                run_model.status = "cancelled"
                run_model.save()

        except Exception as e:
            logger.error(f"Error cancelling run: {e}")

        return "cancelled"

    def handle_time_selection(self, available_times, user_request, patient_code):
        """
        Умный алгоритм выбора подходящего времени из доступных слотов.
        """
        logger.info(f"Running time selection algorithm with {len(available_times)} available times")

        # Если есть только одно время - просто возвращаем его
        if len(available_times) == 1:
            return available_times[0]

        # Анализируем запрос пациента для выявления предпочтений
        request_lower = user_request.lower()

        # Проверяем разные временные предпочтения
        morning_keywords = ["утр", "утром", "с утра", "на утро", "пораньше", "рано", "раннее"]
        lunch_keywords = ["обед", "днем", "дневн", "полдень", "днём", "в обед", "на обед"]
        evening_keywords = ["вечер", "ужин", "вечером", "на ужин", "к ужину", "поздно", "попозже", "позднее"]

        # Нормализуем времена для сравнения (убираем даты, оставляем только время)
        normalized_times = []
        for time_slot in available_times:
            if isinstance(time_slot, str):
                # Извлекаем только часть времени
                if " " in time_slot:
                    time_slot = time_slot.split(" ")[-1]
                normalized_times.append(time_slot)

        # Если ничего не нашли, используем оригинальный список
        if not normalized_times:
            normalized_times = available_times

        # Сортируем времена в хронологическом порядке
        sorted_times = sorted(normalized_times)

        # Определяем временные диапазоны
        morning_times = [t for t in sorted_times if self._is_time_in_range(t, 9, 0, 12, 0)]
        afternoon_times = [t for t in sorted_times if self._is_time_in_range(t, 12, 0, 16, 0)]
        evening_times = [t for t in sorted_times if self._is_time_in_range(t, 16, 0, 21, 0)]

        # Анализируем запрос и выбираем подходящее время
        if any(keyword in request_lower for keyword in morning_keywords) and morning_times:
            logger.info(f"Selected morning time: {morning_times[0]}")
            return morning_times[0]
        elif any(keyword in request_lower for keyword in lunch_keywords) and afternoon_times:
            logger.info(f"Selected afternoon time: {afternoon_times[0]}")
            return afternoon_times[0]
        elif any(keyword in request_lower for keyword in evening_keywords) and evening_times:
            logger.info(f"Selected evening time: {evening_times[0]}")
            return evening_times[0]

        # По умолчанию выбираем первое доступное время
        logger.info(f"No specific time preference detected, selecting earliest: {sorted_times[0]}")
        return sorted_times[0]

    def _is_time_in_range(self, time_str, start_hour, start_minute, end_hour, end_minute):
        """Проверяет, находится ли время в указанном диапазоне"""
        try:
            # Извлекаем время из строки
            if isinstance(time_str, str) and ":" in time_str:
                if " " in time_str:
                    time_str = time_str.split(" ")[-1]

                # Разбиваем на часы и минуты
                hour, minute = map(int, time_str.split(':'))

                # Переводим в минуты для сравнения
                time_minutes = hour * 60 + minute
                start_minutes = start_hour * 60 + start_minute
                end_minutes = end_hour * 60 + end_minute

                return start_minutes <= time_minutes < end_minutes
            return False
        except Exception as e:
            logger.error(f"Error checking time range: {e}")
            return False

    def _get_last_user_message(self, thread_id):
        """Получает последнее сообщение пользователя из треда"""
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
            logger.error(f"Error getting last user message: {e}")
            return ""

    def _extract_time_of_day(self, text):
        """Extract time of day references from text"""
        if any(kw in text for kw in ["утр", "утром", "с утра", "на утро", "рано"]):
            return "утро"
        elif any(kw in text for kw in ["обед", "днем", "дневн", "полдень"]):
            return "обед"
        elif any(kw in text for kw in ["вечер", "ужин", "вечером", "поздн"]):
            return "вечер"
        return None

    def _parse_relative_date(self, text):
        """Parse relative date references like 'через неделю'"""
        today = datetime.now()
        text = text.lower()

        # Extract number of days/weeks/months
        match = re.search(r'через (\d+) (день|дня|дней|недел[юяи]|месяц|месяца|месяцев)', text)
        if match:
            number = int(match.group(1))
            unit = match.group(2)

            if "день" in unit or "дня" in unit or "дней" in unit:
                target_date = today + timedelta(days=number)
            elif "недел" in unit:
                target_date = today + timedelta(weeks=number)
            elif "месяц" in unit or "месяца" in unit or "месяцев" in unit:
                # Approximate a month as 30 days
                target_date = today + timedelta(days=number * 30)

            return target_date.strftime("%Y-%m-%d")

        # Handle "через неделю" without number
        if "через неделю" in text:
            target_date = today + timedelta(weeks=1)
            return target_date.strftime("%Y-%m-%d")

        return text  # Return original if no match

    def _is_time_earlier(self, time_str, reference_time):
        """Check if time is earlier than reference time"""
        try:
            if len(time_str.split()) > 1:
                time_str = time_str.split()[1]  # Extract time part
            if len(reference_time.split()) > 1:
                reference_time = reference_time.split()[1]  # Extract time part

            time_parts = time_str.split(':')
            ref_parts = reference_time.split(':')

            time_minutes = int(time_parts[0]) * 60 + int(time_parts[1])
            ref_minutes = int(ref_parts[0]) * 60 + int(ref_parts[1])

            return time_minutes < ref_minutes
        except Exception:
            return False

    def _is_time_later(self, time_str, reference_time):
        """Check if time is later than reference time"""
        try:
            if len(time_str.split()) > 1:
                time_str = time_str.split()[1]  # Extract time part
            if len(reference_time.split()) > 1:
                reference_time = reference_time.split()[1]  # Extract time part

            time_parts = time_str.split(':')
            ref_parts = reference_time.split(':')

            time_minutes = int(time_parts[0]) * 60 + int(time_parts[1])
            ref_minutes = int(ref_parts[0]) * 60 + int(ref_parts[1])

            return time_minutes > ref_minutes
        except Exception:
            return False
