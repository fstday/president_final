import os
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Union

from django.conf import settings
from openai import OpenAI
from openai.types.beta.threads import Run

from reminder.models import Assistant, Thread, Run as RunModel, Patient, Appointment

logger = logging.getLogger(__name__)


class AssistantClient:
    """
    Клиент для работы с OpenAI Assistant API.
    Обеспечивает взаимодействие между пациентами и системой записи на прием.
    """

    def __init__(self):
        self.client = OpenAI(api_key=settings.OPEN_AI_API_KEY)

    def get_or_create_thread(self, appointment_id: int) -> Thread:
        """
        Получает существующий или создает новый поток для диалога с пациентом
        """
        # Проверяем, есть ли уже тред для этой записи
        try:
            appointment = Appointment.objects.get(appointment_id=appointment_id)
            thread = Thread.objects.filter(
                appointment_id=appointment_id,
                is_expired=False
            ).first()

            if thread:
                # Проверяем, не истек ли тред
                if thread.is_expired():
                    # Создаем новый тред
                    openai_thread = self.client.beta.threads.create()
                    thread.thread_id = openai_thread.id
                    thread.expires_at = datetime.now() + timedelta(hours=24)
                    thread.save()
                return thread

            # Если треда нет, создаем новый
            assistant = Assistant.objects.first()  # Берем первого ассистента из БД
            if not assistant:
                raise ValueError("Assistants not found in database")

            openai_thread = self.client.beta.threads.create()
            thread = Thread.objects.create(
                thread_id=openai_thread.id,
                assistant=assistant,
                appointment_id=appointment_id
            )
            return thread

        except Appointment.DoesNotExist:
            raise ValueError(f"Appointment with ID {appointment_id} not found")
        except Exception as e:
            logger.error(f"Error creating thread: {e}")
            raise

    def add_message_to_thread(self, thread_id: str, content: str, role: str = "user") -> dict:
        """
        Добавляет сообщение в тред
        """
        try:
            message = self.client.beta.threads.messages.create(
                thread_id=thread_id,
                role=role,
                content=content
            )
            return message
        except Exception as e:
            logger.error(f"Error adding message to thread: {e}")
            raise

    def run_assistant(self, thread: Thread, appointment: Appointment) -> RunModel:
        """
        Запускает ассистента для обработки сообщений в треде
        """
        try:
            # Получаем данные о пациенте и записи для контекста
            patient = appointment.patient

            # Собираем информацию о записи для контекста
            context = {
                "appointment_id": appointment.appointment_id,
                "patient_name": patient.full_name,
                "patient_code": patient.patient_code,
                "appointment_time": appointment.start_time.strftime("%Y-%m-%d %H:%M"),
                "doctor_name": appointment.doctor.full_name if appointment.doctor else "Не указан",
                "clinic_name": appointment.clinic.name if appointment.clinic else "Не указана"
            }

            # Создаем запуск ассистента с дополнительными инструкциями на основе контекста
            openai_run = self.client.beta.threads.runs.create(
                thread_id=thread.thread_id,
                assistant_id=thread.assistant.assistant_id,
                instructions=f"""
                Контекст текущего разговора:
                Пациент: {context['patient_name']} (ID: {context['patient_code']})
                Запись: {context['appointment_id']} на {context['appointment_time']}
                Врач: {context['doctor_name']}
                Клиника: {context['clinic_name']}

                Используйте эту информацию при обработке запроса пользователя.
                """
            )

            # Сохраняем информацию о запуске в БД
            run = RunModel.objects.create(
                run_id=openai_run.id,
                status=openai_run.status
            )

            # Обновляем поток, связывая его с текущим запуском
            thread.current_run = run
            thread.save()

            return run

        except Exception as e:
            logger.error(f"Error running assistant: {e}")
            raise

    def handle_function_calls(self, run_id: str, thread_id: str):
        """
        Обрабатывает вызовы функций от ассистента
        """
        try:
            run = self.client.beta.threads.runs.retrieve(
                thread_id=thread_id,
                run_id=run_id
            )

            # Если требуется действие
            if run.status == "requires_action" and run.required_action:
                tool_calls = run.required_action.submit_tool_outputs.tool_calls

                tool_outputs = []
                for tool_call in tool_calls:
                    function_name = tool_call.function.name
                    function_args = json.loads(tool_call.function.arguments)

                    # Получаем результат вызова функции
                    result = self._call_function(function_name, function_args)

                    tool_outputs.append({
                        "tool_call_id": tool_call.id,
                        "output": json.dumps(result)
                    })

                # Отправляем результаты вызова функций
                if tool_outputs:
                    self.client.beta.threads.runs.submit_tool_outputs(
                        thread_id=thread_id,
                        run_id=run_id,
                        tool_outputs=tool_outputs
                    )

            return run.status

        except Exception as e:
            logger.error(f"Error handling function calls: {e}")
            raise

    def _call_function(self, function_name: str, function_args: dict) -> dict:
        """
        Вызывает соответствующую функцию на основе имени
        """
        # Импортируем функции только при необходимости вызова
        from reminder.infoclinica_requests.schedule.delete_reception_for_patient import delete_reception_for_patient
        from reminder.infoclinica_requests.schedule.reserve_reception_for_patient import reserve_reception_for_patient
        from reminder.infoclinica_requests.schedule.appointment_time_for_patient import appointment_time_for_patient
        from reminder.infoclinica_requests.schedule.which_time_in_certain_day import which_time_in_certain_day

        try:
            if function_name == "delete_reception_for_patient":
                patient_id = function_args.get("patient_id")
                return delete_reception_for_patient(patient_id)

            elif function_name == "reserve_reception_for_patient":
                patient_id = function_args.get("patient_id")
                date_from_patient = function_args.get("date_from_patient")
                trigger_id = function_args.get("trigger_id", 1)
                return reserve_reception_for_patient(patient_id, date_from_patient, trigger_id)

            elif function_name == "appointment_time_for_patient":
                patient_code = function_args.get("patient_code")
                year_from_patient_for_returning = function_args.get("year_from_patient_for_returning")
                return appointment_time_for_patient(patient_code, year_from_patient_for_returning)

            elif function_name == "which_time_in_certain_day":
                reception_id = function_args.get("reception_id")
                date_time = function_args.get("date_time")
                return which_time_in_certain_day(reception_id, date_time)

            else:
                logger.warning(f"Unknown function: {function_name}")
                return {"status": "error", "message": f"Unknown function: {function_name}"}

        except Exception as e:
            logger.error(f"Error calling function {function_name}: {e}")
            return {"status": "error", "message": str(e)}

    def get_messages(self, thread_id: str, limit: int = 10) -> List[dict]:
        """
        Получает список сообщений из треда
        """
        try:
            messages = self.client.beta.threads.messages.list(
                thread_id=thread_id,
                limit=limit
            )
            return messages.data
        except Exception as e:
            logger.error(f"Error getting messages: {e}")
            raise

    def wait_for_run_completion(self, thread_id: str, run_id: str, timeout: int = 60) -> str:
        """
        Ожидает завершения запуска ассистента с периодической проверкой статуса
        """
        import time

        start_time = time.time()
        while time.time() - start_time < timeout:
            run = self.client.beta.threads.runs.retrieve(
                thread_id=thread_id,
                run_id=run_id
            )

            # Обрабатываем вызовы функций при необходимости
            if run.status == "requires_action":
                self.handle_function_calls(run_id, thread_id)

            # Обновляем статус в БД
            run_model = RunModel.objects.filter(run_id=run_id).first()
            if run_model:
                run_model.status = run.status
                run_model.save()

            # Проверяем, завершен ли запуск
            if run.status in ["completed", "failed", "cancelled", "expired"]:
                return run.status

            # Ждем перед следующим запросом
            time.sleep(1)

        # Если истекло время, отменяем запуск
        self.client.beta.threads.runs.cancel(
            thread_id=thread_id,
            run_id=run_id
        )

        # Обновляем статус в БД
        run_model = RunModel.objects.filter(run_id=run_id).first()
        if run_model:
            run_model.status = "cancelled"
            run_model.save()

        return "cancelled"
