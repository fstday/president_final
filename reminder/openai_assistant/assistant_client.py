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
        Runs the assistant with proper instructions to ensure function calling

        Args:
            thread: Thread object
            appointment: Appointment object

        Returns:
            RunModel: Run model object
        """
        try:
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

            # Get current date and tomorrow's date
            today = datetime.now().strftime("%Y-%m-%d")
            tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

            # Create explicit instructions with examples
            instructions = f"""
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

            # КОНКРЕТНЫЕ ПРИМЕРЫ ВЫЗОВОВ ФУНКЦИЙ:

            "Какие свободные окошки на сегодня" → which_time_in_certain_day(patient_code="{patient_code}", date_time="today")

            "Какие окошки на завтра" → which_time_in_certain_day(patient_code="{patient_code}", date_time="tomorrow")

            "Какие окна на пятницу" → which_time_in_certain_day(patient_code="{patient_code}", date_time="2025-03-22")

            "Когда у меня запись" → appointment_time_for_patient(patient_code="{patient_code}")

            "Во сколько мне приходить" → appointment_time_for_patient(patient_code="{patient_code}")

            "Хочу записаться на завтра в 15:00" → reserve_reception_for_patient(patient_id="{patient_code}", date_from_patient="{tomorrow} 15:00", trigger_id=1)

            "Перенесите на сегодня на вечер" → reserve_reception_for_patient(patient_id="{patient_code}", date_from_patient="{today} 18:00", trigger_id=1)

            "Отмените мою запись" → delete_reception_for_patient(patient_id="{patient_code}")

            # ВАЖНАЯ ИНФОРМАЦИЯ О ПАЦИЕНТЕ И ПРИЕМЕ:

            - Текущий пациент: {patient.full_name} (ID: {patient_code})
            - Запись на прием: {appointment.appointment_id}
            - Время приема: {appointment_time}
            - Врач: {doctor_name}
            - Клиника: {clinic_name}

            # ЗАПРЕЩЕНО использовать текстовые ответы для вышеперечисленных запросов!
            """

            # Create assistant run
            openai_run = self.client.beta.threads.runs.create(
                thread_id=thread.thread_id,
                assistant_id=thread.assistant.assistant_id,
                instructions=instructions
            )

            # Save run in database
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

    def handle_function_calls(self, run_id: str, thread_id: str) -> Any:
        """
        Обрабатывает функциональные вызовы от ассистента и форматирует ответы
        в соответствии с требованиями ACS.

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
                    raw_result = self._call_function(function_name, function_args, thread_id)
                    logger.info(f"Сырой результат функции: {raw_result}")

                    # Форматируем результат для ACS
                    formatted_result = self._format_for_acs(function_name, function_args, raw_result)
                    logger.info(f"Отформатированный результат для ACS: {formatted_result}")

                    tool_outputs.append({
                        "tool_call_id": tool_call.id,
                        "output": json.dumps(formatted_result)
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

    def _format_for_acs(self, function_name: str, function_args: dict, result: dict) -> dict:
        """
        Форматирует результаты функций в соответствии с требованиями ACS.

        Args:
            function_name: Имя вызванной функции
            function_args: Аргументы функции
            result: Исходный результат функции

        Returns:
            dict: Отформатированный результат
        """

        # Вспомогательные функции для форматирования
        def get_date_relation(date_str):
            """Определяет отношение даты к текущему дню (сегодня/завтра/другое)"""
            try:
                if date_str == "today":
                    return "today"
                if date_str == "tomorrow":
                    return "tomorrow"

                # Извлекаем часть даты, если есть время
                if ' ' in date_str:
                    date_str = date_str.split(' ')[0]

                date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
                today = datetime.now().date()
                tomorrow = today + timedelta(days=1)

                if date_obj == today:
                    return "today"
                elif date_obj == tomorrow:
                    return "tomorrow"
                return None
            except Exception:
                return None

        def format_date_info(date_str):
            """Форматирует дату для ответа ACS"""
            try:
                # Парсим строку даты
                if date_str == "today":
                    date_obj = datetime.now()
                elif date_str == "tomorrow":
                    date_obj = datetime.now() + timedelta(days=1)
                else:
                    if ' ' in date_str:  # Есть время
                        date_str = date_str.split(' ')[0]
                    date_obj = datetime.strptime(date_str, "%Y-%m-%d")

                # Соответствия месяцев
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
                logger.error(f"Ошибка форматирования даты: {e}")
                return {
                    "date": "Неизвестная дата",
                    "date_kz": "Белгісіз күн",
                    "weekday": "Неизвестный день",
                    "weekday_kz": "Белгісіз күн",
                }

        try:
            # Значения по умолчанию
            formatted = {
                "status": "error",
                "message": "Произошла неизвестная ошибка"
            }

            # 1. which_time_in_certain_day - доступные слоты
            if function_name == "which_time_in_certain_day":
                date_time = function_args.get("date_time", "")
                date_relation = get_date_relation(date_time)
                date_info = format_date_info(date_time)

                # Проверяем, есть ли ошибка в результате
                if isinstance(result, dict) and result.get("status", "").startswith("error"):
                    if "no available times" in result.get("message", "").lower() or "не найдено" in result.get(
                            "message", "").lower():
                        status = "error_empty_windows"
                        if date_relation == "today":
                            status = "error_empty_windows_today"
                        elif date_relation == "tomorrow":
                            status = "error_empty_windows_tomorrow"

                        formatted = {
                            "status": status,
                            "message": f"Свободных приемов {'на сегодня' if date_relation == 'today' else 'на завтра' if date_relation == 'tomorrow' else ''} не найдено."
                        }

                        if date_relation == "today":
                            formatted["day"] = "сегодня"
                            formatted["day_kz"] = "бүгін"
                        elif date_relation == "tomorrow":
                            formatted["day"] = "завтра"
                            formatted["day_kz"] = "ертең"

                        return formatted

                # Извлекаем доступные времена
                times = []
                if isinstance(result, dict):
                    # Проверяем разные форматы времен
                    if "all_available_times" in result and isinstance(result["all_available_times"], list):
                        times = result["all_available_times"]
                    elif "time_1" in result and result["time_1"]:
                        for i in range(1, 4):
                            key = f"time_{i}"
                            if key in result and result[key]:
                                times.append(result[key])
                    elif "first_time" in result and result["first_time"]:
                        for key in ["first_time", "second_time", "third_time"]:
                            if key in result and result[key]:
                                times.append(result[key])

                # Очищаем времена (убираем дату, если она есть)
                clean_times = []
                for t in times:
                    if isinstance(t, str) and ' ' in t:
                        clean_times.append(t.split(' ')[1])  # Берем только время
                    else:
                        clean_times.append(t)

                # Определяем статус в зависимости от количества времен
                if len(clean_times) == 0:
                    status = "error_empty_windows"
                    if date_relation == "today":
                        status = "error_empty_windows_today"
                    elif date_relation == "tomorrow":
                        status = "error_empty_windows_tomorrow"

                    formatted = {
                        "status": status,
                        "message": f"Свободных приемов {'на сегодня' if date_relation == 'today' else 'на завтра' if date_relation == 'tomorrow' else ''} не найдено."
                    }

                elif len(clean_times) == 1:
                    status = "only_first_time"
                    if date_relation == "today":
                        status = "only_first_time_today"
                    elif date_relation == "tomorrow":
                        status = "only_first_time_tomorrow"

                    formatted = {
                        "status": status,
                        "date": date_info["date"],
                        "date_kz": date_info["date_kz"],
                        "specialist_name": result.get("doctor_name", "Специалист"),
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

                    formatted = {
                        "status": status,
                        "date": date_info["date"],
                        "date_kz": date_info["date_kz"],
                        "specialist_name": result.get("doctor_name", "Специалист"),
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

                    formatted = {
                        "status": status,
                        "date": date_info["date"],
                        "date_kz": date_info["date_kz"],
                        "specialist_name": result.get("doctor_name", "Специалист"),
                        "weekday": date_info["weekday"],
                        "weekday_kz": date_info["weekday_kz"],
                        "first_time": clean_times[0],
                        "second_time": clean_times[1] if len(clean_times) > 1 else None,
                        "third_time": clean_times[2] if len(clean_times) > 2 else None
                    }

                # Добавляем информацию о дне, если это сегодня/завтра
                if date_relation == "today":
                    formatted["day"] = "сегодня"
                    formatted["day_kz"] = "бүгін"
                elif date_relation == "tomorrow":
                    formatted["day"] = "завтра"
                    formatted["day_kz"] = "ертең"

                # Удаляем None значения
                formatted = {k: v for k, v in formatted.items() if v is not None}

                return formatted

            # 2. appointment_time_for_patient - информация о текущей записи
            elif function_name == "appointment_time_for_patient":
                if isinstance(result, dict):
                    if result.get("status") == "error_no_appointment":
                        return {
                            "status": "error_reception_unavailable",
                            "message": "У пациента нет активных записей на прием"
                        }

                    if "appointment_time" in result and "appointment_date" in result:
                        date_info = format_date_info(result["appointment_date"])
                        date_relation = get_date_relation(result["appointment_date"])

                        formatted = {
                            "status": "success_for_check_info",
                            "specialist_name": result.get("doctor_name", "Специалист"),
                            "date": date_info["date"],
                            "date_kz": date_info["date_kz"],
                            "weekday": date_info["weekday"],
                            "weekday_kz": date_info["weekday_kz"],
                            "time": result["appointment_time"]
                        }

                        if date_relation == "today":
                            formatted["day"] = "сегодня"
                            formatted["day_kz"] = "бүгін"
                        elif date_relation == "tomorrow":
                            formatted["day"] = "завтра"
                            formatted["day_kz"] = "ертең"

                        return formatted

                # Если не получается правильно отформатировать, возвращаем исходник
                return result

            # 3. reserve_reception_for_patient - запись/перенос
            elif function_name == "reserve_reception_for_patient":
                date_from_patient = function_args.get("date_from_patient", "")
                date_relation = get_date_relation(date_from_patient)
                date_info = format_date_info(date_from_patient)

                if isinstance(result, dict):
                    status = result.get("status", "")

                    # Успешный случай
                    if status == "success_schedule":
                        success_status = "success_change_reception"
                        if date_relation == "today":
                            success_status = "success_change_reception_today"
                        elif date_relation == "tomorrow":
                            success_status = "success_change_reception_tomorrow"

                        # Извлекаем время, если передан полный datetime
                        time_value = result.get("time", "")
                        if isinstance(time_value, str) and ' ' in time_value:
                            time_value = time_value.split(' ')[1]

                        formatted = {
                            "status": success_status,
                            "date": date_info["date"],
                            "date_kz": date_info["date_kz"],
                            "specialist_name": result.get("specialist_name", "Специалист"),
                            "weekday": date_info["weekday"],
                            "weekday_kz": date_info["weekday_kz"],
                            "time": time_value
                        }

                        if date_relation == "today":
                            formatted["day"] = "сегодня"
                            formatted["day_kz"] = "бүгін"
                        elif date_relation == "tomorrow":
                            formatted["day"] = "завтра"
                            formatted["day_kz"] = "ертең"

                        return formatted

                    # Случай ошибки с предложением альтернатив
                    elif status == "suggest_times" and "suggested_times" in result:
                        suggested_times = result["suggested_times"]

                        # Очищаем времена (извлекаем часть времени, если надо)
                        clean_times = []
                        for t in suggested_times:
                            if isinstance(t, str) and ' ' in t:
                                clean_times.append(t.split(' ')[1])
                            else:
                                clean_times.append(t)

                        # Определяем статус в зависимости от количества времен
                        if len(clean_times) == 0:
                            error_status = "error_empty_windows"
                            if date_relation == "today":
                                error_status = "error_empty_windows_today"
                            elif date_relation == "tomorrow":
                                error_status = "error_empty_windows_tomorrow"

                            formatted = {
                                "status": error_status,
                                "message": f"Свободных приемов {'на сегодня' if date_relation == 'today' else 'на завтра' if date_relation == 'tomorrow' else ''} не найдено."
                            }

                        elif len(clean_times) == 1:
                            error_status = "change_only_first_time"
                            if date_relation == "today":
                                error_status = "change_only_first_time_today"
                            elif date_relation == "tomorrow":
                                error_status = "change_only_first_time_tomorrow"

                            formatted = {
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

                            formatted = {
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

                            formatted = {
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

                        # Добавляем информацию о дне
                        if date_relation == "today":
                            formatted["day"] = "сегодня"
                            formatted["day_kz"] = "бүгін"
                        elif date_relation == "tomorrow":
                            formatted["day"] = "завтра"
                            formatted["day_kz"] = "ертең"

                        # Удаляем None значения
                        formatted = {k: v for k, v in formatted.items() if v is not None}

                        return formatted

                    # Ошибка с неправильной датой
                    elif "date" in result.get("message", "").lower():
                        return {
                            "status": "error_change_reception_bad_date",
                            "data": result.get("message", "Неверный формат даты")
                        }

                # Если не получается правильно отформатировать, возвращаем исходник
                return result

            # 4. delete_reception_for_patient - отмена записи
            elif function_name == "delete_reception_for_patient":
                if isinstance(result, dict):
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

                # Если не получается правильно отформатировать, возвращаем исходник
                return result

            # Неизвестная функция - возвращаем как есть
            else:
                return result

        except Exception as e:
            logger.error(f"Ошибка форматирования ответа для ACS: {e}", exc_info=True)
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

    def wait_for_run_completion(self, thread_id: str, run_id: str, timeout: int = 60) -> str:
        """
        Waits for completion of an assistant run using streaming API, with proper handling
        of function calls and status mapping for ACS voice robot.

        Args:
            thread_id: Thread ID
            run_id: Run ID
            timeout: Maximum wait time in seconds

        Returns:
            str: Final run status
        """
        import time

        logger.info(f"Waiting for completion of run {run_id} (timeout: {timeout}s)")

        start_time = time.time()

        try:
            # Stream the run updates
            for event in self.client.beta.threads.runs.stream(
                    thread_id=thread_id,
                    run_id=run_id
            ):
                # Check for timeout
                if time.time() - start_time > timeout:
                    logger.warning(f"Run {run_id} exceeded timeout {timeout}s, cancelling")
                    self._cancel_run(thread_id, run_id)
                    return "cancelled"

                # Process different event types
                if hasattr(event, 'event'):
                    event_type = event.event
                    logger.info(f"Received event: {event_type}")

                    if event_type == "thread.run.requires_action":
                        logger.info(f"Run {run_id} requires action")
                        self._update_run_status(run_id, "requires_action")

                        # Handle function calls with proper response formatting for ACS
                        result = self.handle_function_calls(run_id, thread_id)
                        logger.info(f"Function call result: {result}")

                    elif event_type == "thread.run.completed":
                        logger.info(f"Run {run_id} completed")
                        self._update_run_status(run_id, "completed")
                        return "completed"

                    elif event_type in ["thread.run.failed", "thread.run.cancelled", "thread.run.expired"]:
                        logger.info(f"Run {run_id} ended with status: {event_type.split('.')[-1]}")
                        self._update_run_status(run_id, event_type.split('.')[-1])
                        return event_type.split('.')[-1]

                    elif event_type == "thread.run.in_progress":
                        logger.info(f"Run {run_id} in progress")
                        self._update_run_status(run_id, "in_progress")

            # If we exit the loop, check the final status
            final_status = self._check_run_status(thread_id, run_id)
            return final_status

        except Exception as e:
            logger.error(f"Error streaming run updates: {str(e)}")
            # Fall back to polling method
            return self._poll_run_completion(thread_id, run_id, timeout - (time.time() - start_time))

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

        Args:
            thread_id: Thread ID
            run_id: Run ID
        """
        try:
            self.client.beta.threads.runs.cancel(
                thread_id=thread_id,
                run_id=run_id
            )
            self._update_run_status(run_id, "cancelled")
        except Exception as e:
            logger.error(f"Error cancelling run: {str(e)}")

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