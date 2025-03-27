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
        Получает существующий тред или создает новый для диалога с пациентом.

        Args:
            appointment_id: ID записи на прием

        Returns:
            Thread: Объект треда для диалога
        """
        try:
            # Проверяем, существует ли запись на прием
            appointment = Appointment.objects.get(appointment_id=appointment_id)

            # Ищем активный тред для этой записи
            thread = Thread.objects.filter(
                appointment_id=appointment_id,
                expires_at__gt=timezone.now()
            ).first()

            if thread:
                logger.info(f"Найден существующий тред {thread.thread_id} для записи {appointment_id}")
                return thread

            # Если активный тред не найден, создаем новый
            assistant = Assistant.objects.first()  # Получаем первого ассистента из БД
            if not assistant:
                logger.error("Ассистенты не найдены в базе данных")
                raise ValueError("Ассистенты не найдены в базе данных")

            # Создаем тред в OpenAI
            openai_thread = self.client.beta.threads.create()

            # Сохраняем тред в локальной базе данных
            thread = Thread.objects.create(
                thread_id=openai_thread.id,
                order_key=str(appointment_id),
                assistant=assistant,
                appointment_id=appointment_id  # Сохраняем ID записи
            )
            logger.info(f"Создан новый тред {thread.thread_id} для записи {appointment_id}")
            return thread

        except Appointment.DoesNotExist:
            logger.error(f"Запись с ID {appointment_id} не найдена")
            raise ValueError(f"Запись с ID {appointment_id} не найдена")
        except Exception as e:
            logger.error(f"Ошибка при создании/поиске треда: {str(e)}")
            raise

    def add_message_to_thread(self, thread_id: str, content: str, role: str = "user") -> dict:
        """
        Добавляет сообщение в тред.

        Args:
            thread_id: ID треда
            content: Текст сообщения
            role: Роль отправителя (обычно "user")

        Returns:
            dict: Информация о созданном сообщении
        """
        try:
            message = self.client.beta.threads.messages.create(
                thread_id=thread_id,
                role=role,
                content=content
            )
            logger.info(f"Добавлено сообщение от {role} в тред {thread_id}")
            return message
        except Exception as e:
            logger.error(f"Ошибка при добавлении сообщения в тред: {str(e)}")
            raise

    def run_assistant(self, thread: Thread, appointment: Appointment) -> RunModel:
        """
        Запускает ассистента для обработки сообщений в треде.

        Args:
            thread: Объект треда
            appointment: Объект записи на прием

        Returns:
            RunModel: Объект запуска ассистента
        """
        try:
            # Получаем данные о пациенте и записи для контекста
            patient = appointment.patient
            patient_code = patient.patient_code

            # Форматируем время записи
            appointment_time_str = "Не указано"
            if appointment.start_time:
                appointment_time_str = appointment.start_time.strftime("%Y-%m-%d %H:%M")

            # Получаем имя врача
            doctor_name = "Не указан"
            if appointment.doctor:
                doctor_name = appointment.doctor.full_name

            # Получаем название клиники
            clinic_name = "Не указана"
            if appointment.clinic:
                clinic_name = appointment.clinic.name

            # Текущая дата и завтрашняя дата для контекста
            current_date = datetime.now().strftime("%Y-%m-%d")
            tomorrow_date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

            # Получаем расширенные инструкции
            enhanced_instructions = get_enhanced_assistant_prompt()

            # Добавляем контекст текущего разговора
            context_instructions = enhanced_instructions + f"""
                # КОНТЕКСТ ТЕКУЩЕГО РАЗГОВОРА

                ## ДАННЫЕ О ПАЦИЕНТЕ И ЗАПИСИ
                - Пациент: {patient.full_name} (ID: {patient_code})
                - Текущая запись: {appointment.appointment_id} на {appointment_time_str}
                - Врач: {doctor_name}
                - Клиника: {clinic_name}

                ## ОБРАБОТКА ДАТ И ВРЕМЕНИ
                - Сегодняшняя дата: {current_date}
                - Завтрашняя дата: {tomorrow_date}
                - Когда пользователь говорит "сегодня", используй дату {current_date}
                - Когда пользователь говорит "завтра", используй дату {tomorrow_date}

                ## ПАРАМЕТРЫ ДЛЯ ФУНКЦИЙ
                - Для всех функций используй patient_code: "{patient_code}"
                - Для appointment_time_for_patient используй параметр patient_code="{patient_code}"
                - Для which_time_in_certain_day используй параметр patient_code="{patient_code}"
                - Для reserve_reception_for_patient используй параметр patient_id="{patient_code}"
                - Для delete_reception_for_patient используй параметр patient_id="{patient_code}"

                ## ДЕТАЛЬНАЯ КАРТА ВРЕМЕНИ

                Используй эту карту соответствий для определения конкретного времени при запросах пациентов:

                ### Утро (09:00-11:00):
                - "утро", "утром", "с утра", "на утро" → 10:30
                - "пораньше", "рано", "раннее" → 10:30

                ### Обед (12:00-14:00):
                - "обед", "на обед", "в обед" → 13:30
                - "полдень", "в полдень" → 13:30
                - "дневное", "днем" → 13:30

                ### До и после обеда:
                - "после обеда", "послеобеденное", "дневное время" → 15:00
                - "до обеда", "перед обедом", "предобеденное" → 11:00

                ### Вечер (17:00-20:00):
                - "вечер", "вечером", "на вечер" → 18:30
                - "ужин", "на ужин", "к ужину" → 18:30
                - "поздно", "попозже", "позднее" → 18:30
            """

            # Создаем запуск ассистента с дополнительным контекстом
            openai_run = self.client.beta.threads.runs.create(
                thread_id=thread.thread_id,
                assistant_id=thread.assistant.assistant_id,
                instructions=context_instructions
            )

            # Сохраняем информацию о запуске в БД
            run = RunModel.objects.create(
                run_id=openai_run.id,
                status=openai_run.status
            )

            # Обновляем тред с текущим запуском
            thread.current_run = run
            thread.save()

            logger.info(f"Запущен запуск {run.run_id} для треда {thread.thread_id}")
            return run
        except Exception as e:
            logger.error(f"Ошибка при запуске ассистента: {str(e)}", exc_info=True)
            raise

    def handle_function_calls(self, run_id: str, thread_id: str) -> Any:
        """
        Обрабатывает функциональные вызовы от ассистента.

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
                for tool_call in tool_calls:
                    function_name = tool_call.function.name
                    function_args = json.loads(tool_call.function.arguments)

                    logger.info(f"Обнаружен вызов функции: {function_name} с аргументами: {function_args}")

                    # Получаем результат вызова функции
                    result = self._call_function(function_name, function_args, thread_id)
                    logger.info(f"Результат функции: {result}")

                    tool_outputs.append({
                        "tool_call_id": tool_call.id,
                        "output": json.dumps(result)
                    })

                # Отправляем результаты вызовов функций
                if tool_outputs:
                    self.client.beta.threads.runs.submit_tool_outputs(
                        thread_id=thread_id,
                        run_id=run_id,
                        tool_outputs=tool_outputs
                    )
                    logger.info(f"Отправлено {len(tool_outputs)} результатов функций для запуска {run_id}")
                    return tool_outputs
                else:
                    logger.warning("Нет результатов функций для отправки")
                    return []

            return run.status

        except Exception as e:
            logger.error(f"Ошибка при обработке вызовов функций: {str(e)}", exc_info=True)
            raise

    def _call_function(self, function_name: str, function_args: dict, thread_id: str = None) -> dict:
        """
        Улучшенная функция для правильного вызова функций управления записями на прием.

        Args:
            function_name: Имя функции
            function_args: Аргументы функции
            thread_id: ID треда (опционально)

        Returns:
            dict: Результат вызова функции
        """
        # Импортируем функции
        from reminder.infoclinica_requests.schedule.delete_reception_for_patient import delete_reception_for_patient
        from reminder.infoclinica_requests.schedule.reserve_reception_for_patient import reserve_reception_for_patient
        from reminder.infoclinica_requests.schedule.appointment_time_for_patient import appointment_time_for_patient
        from reminder.infoclinica_requests.schedule.which_time_in_certain_day import which_time_in_certain_day
        from datetime import datetime, timedelta

        try:
            logger.info(f"Вызываем функцию {function_name} с аргументами: {function_args}")

            # Обработка относительных дат
            if function_name == "reserve_reception_for_patient":
                date_from_patient = function_args.get("date_from_patient", "")
                patient_id = function_args.get("patient_id")

                # Обработка относительных дат
                if isinstance(date_from_patient, str) and "через" in date_from_patient.lower():
                    # Парсинг "через неделю", "через 3 дня" и т.д.
                    relative_date = self._parse_relative_date(date_from_patient)
                    if relative_date:
                        function_args["date_from_patient"] = relative_date

                # Обработка указаний времени суток
                if isinstance(date_from_patient, str) and not re.search(r'\d{1,2}:\d{2}', date_from_patient):
                    # Проверяем наличие указаний на время суток
                    date_part = date_from_patient.split()[0] if ' ' in date_from_patient else date_from_patient
                    time_of_day = self._extract_time_of_day(date_from_patient.lower())
                    if time_of_day:
                        # Сопоставляем время суток с конкретным часом
                        specific_time = ""
                        if "утр" in time_of_day:
                            specific_time = "10:30"
                        elif "обед" in time_of_day:
                            specific_time = "13:30"
                        elif "вечер" in time_of_day or "ужин" in time_of_day:
                            specific_time = "18:30"

                        if specific_time:
                            function_args["date_from_patient"] = f"{date_part} {specific_time}"

            # СЛУЧАЙ 1: Удаление записи
            if function_name == "delete_reception_for_patient":
                patient_id = function_args.get("patient_id")
                logger.info(f"Удаляем запись для пациента {patient_id}")
                return delete_reception_for_patient(patient_id)

            # СЛУЧАЙ 2: Запись/перенос записи
            elif function_name == "reserve_reception_for_patient":
                patient_id = function_args.get("patient_id")
                date_from_patient = function_args.get("date_from_patient", "")
                trigger_id = function_args.get("trigger_id", 1)

                # Process relative dates
                if isinstance(date_from_patient, str) and "через" in date_from_patient.lower():
                    # Parsing "через неделю", "через 3 дня" etc.
                    relative_date = self._parse_relative_date(date_from_patient)
                    if relative_date:
                        function_args["date_from_patient"] = relative_date

                # Handle time of day references
                if isinstance(date_from_patient, str) and not re.search(r'\d{1,2}:\d{2}', date_from_patient):
                    # Check for time of day references
                    date_part = date_from_patient.split()[0] if ' ' in date_from_patient else date_from_patient
                    time_of_day = self._extract_time_of_day(date_from_patient.lower())
                    if time_of_day:
                        # Map time of day to specific hour
                        specific_time = ""
                        if "утр" in time_of_day:
                            specific_time = "10:30"
                        elif "обед" in time_of_day:
                            specific_time = "13:30"
                        elif "вечер" in time_of_day or "ужин" in time_of_day:
                            specific_time = "18:30"

                        if specific_time:
                            function_args["date_from_patient"] = f"{date_part} {specific_time}"

                # ADD THIS NEW SECTION FOR TIME ROUNDING:
                # Process and round the appointment time if needed
                if isinstance(date_from_patient, str) and " " in date_from_patient:
                    # Split date and time
                    date_parts = date_from_patient.split()
                    date_part = date_parts[0]
                    time_part = date_parts[1] if len(date_parts) > 1 else ""

                    if time_part and ":" in time_part:
                        # Round time to nearest half-hour
                        rounded_time = self._round_to_nearest_half_hour(time_part)

                        if rounded_time != time_part:
                            logger.info(f"Rounded appointment time from {time_part} to {rounded_time}")

                        # Update the argument with rounded time
                        function_args["date_from_patient"] = f"{date_part} {rounded_time}"

                # Check mandatory parameters
                if not patient_id:
                    return {
                        "status": "error",
                        "message": "Patient code not found for this patient"
                    }

                logger.info(
                    f"Creating/rescheduling appointment for patient {patient_id} at {function_args['date_from_patient']}")
                return reserve_reception_for_patient(patient_id, function_args["date_from_patient"], trigger_id)

            # СЛУЧАЙ 3: Получение информации о текущей записи
            elif function_name == "appointment_time_for_patient":
                patient_code = function_args.get("patient_code")
                year_from_patient_for_returning = function_args.get("year_from_patient_for_returning")

                if not patient_code:
                    return {
                        "status": "error",
                        "message": "Не указан обязательный параметр patient_code"
                    }

                logger.info(f"Получаем информацию о текущей записи для пациента {patient_code}")
                return appointment_time_for_patient(patient_code, year_from_patient_for_returning)

            # СЛУЧАЙ 4: Получение доступных временных слотов
            elif function_name == "which_time_in_certain_day":
                patient_code = function_args.get("patient_code")
                date_time = function_args.get("date_time")

                # Проверки параметров
                if not patient_code or not date_time:
                    return {
                        "status": "error",
                        "message": "Не указаны обязательные параметры (patient_code или date_time)"
                    }

                # Преобразование "today" в текущую дату
                if date_time.lower() == "today":
                    date_time = datetime.now().strftime("%Y-%m-%d")
                # Преобразование "tomorrow" в завтрашнюю дату
                elif date_time.lower() == "tomorrow":
                    date_time = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

                logger.info(f"Получаем доступные временные слоты для пациента {patient_code} на дату {date_time}")

                # Получаем последнее сообщение пользователя
                user_request = self._get_last_user_message(thread_id) if thread_id else ""
                logger.info(f"Запрос пользователя для which_time_in_certain_day: {user_request}")

                # Проверяем, содержит ли запрос ключевые слова для записи
                scheduling_keywords = [
                    "запиши", "запишите", "записать", "записаться",
                    "назначь", "назначьте", "оформи", "оформите",
                    "хочу на", "хочу записаться", "хочу запись",
                    "сделай", "сделайте", "забронируй", "бронь"
                ]

                is_scheduling_request = any(keyword in user_request.lower() for keyword in scheduling_keywords)
                logger.info(f"Запрос на запись: {is_scheduling_request}")

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

                        # Форматируем время для записи
                        formatted_datetime = selected_time

                        # Если время без даты - добавляем дату
                        if ' ' not in selected_time and ':' in selected_time:
                            formatted_datetime = f"{date_time} {selected_time}"

                        logger.info(f"Автоматическая запись: {formatted_datetime}")

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
                logger.warning(f"Неизвестная функция: {function_name}")
                return {"status": "error", "message": f"Неизвестная функция: {function_name}"}

        except Exception as e:
            logger.error(f"Ошибка вызова функции {function_name}: {str(e)}", exc_info=True)
            return {"status": "error", "message": str(e)}

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

    def wait_for_run_completion(self, thread_id: str, run_id: str, timeout: int = 60) -> str:
        """
        Ожидает завершения запуска ассистента с периодическими проверками статуса.

        Args:
            thread_id: ID треда
            run_id: ID запуска
            timeout: Время ожидания в секундах

        Returns:
            str: Статус запуска
        """
        import time

        start_time = time.time()
        poll_interval = 1.0  # Начинаем с интервала в 1 секунду

        logger.info(f"Ожидаем завершения запуска {run_id} (таймаут: {timeout}с)")

        while time.time() - start_time < timeout:
            try:
                run = self.client.beta.threads.runs.retrieve(
                    thread_id=thread_id,
                    run_id=run_id
                )

                # Обрабатываем вызовы функций при необходимости
                if run.status == "requires_action":
                    logger.info(f"Запуск {run_id} требует действия")
                    result = self.handle_function_calls(run_id, thread_id)
                    logger.info(f"Результат вызова функции: {result}")

                # Обновляем статус в БД
                run_model = RunModel.objects.filter(run_id=run_id).first()
                if run_model and run_model.status != run.status:
                    run_model.status = run.status
                    run_model.save()
                    logger.info(f"Обновлен статус запуска на {run.status}")

                # Проверяем, завершен ли запуск
                if run.status in ["completed", "failed", "cancelled", "expired"]:
                    logger.info(f"Запуск {run_id} завершен со статусом: {run.status}")
                    return run.status

                # Используем экспоненциальное увеличение интервала (до 5 секунд)
                poll_interval = min(5.0, poll_interval * 1.5)
                time.sleep(poll_interval)

            except Exception as e:
                logger.error(f"Ошибка проверки статуса запуска: {str(e)}")
                time.sleep(poll_interval)

        # Если таймаут, отменяем запуск
        logger.warning(f"Запуск {run_id} превысил таймаут {timeout}с, отменяем")
        try:
            self.client.beta.threads.runs.cancel(
                thread_id=thread_id,
                run_id=run_id
            )

            # Обновляем статус в БД
            run_model = RunModel.objects.filter(run_id=run_id).first()
            if run_model:
                run_model.status = "cancelled"
                run_model.save()

        except Exception as e:
            logger.error(f"Ошибка отмены запуска: {str(e)}")

        return "cancelled"

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