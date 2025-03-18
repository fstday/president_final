import os
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Union
from django.utils import timezone
from django.conf import settings
from openai import OpenAI

from reminder.models import Assistant, Thread, Run as RunModel, Patient, Appointment, QueueInfo

logger = logging.getLogger(__name__)


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

            # Create context instructions
            context_instructions = f"""
            ВАЖНО: Всегда используйте функции вместо текстовых ответов в следующих случаях:

            1. Когда пользователь спрашивает о свободных окошках или времени (пример: "Какие свободные окошки на сегодня") - 
               ВСЕГДА вызывайте функцию which_time_in_certain_day с параметрами reception_id и date_time

            2. Когда пользователь интересуется своей текущей записью - 
               ВСЕГДА вызывайте функцию appointment_time_for_patient с параметром patient_code

            3. Когда пользователь хочет записаться или перенести запись - 
               ВСЕГДА вызывайте функцию reserve_reception_for_patient

            4. Когда пользователь хочет отменить запись - 
               ВСЕГДА вызывайте функцию delete_reception_for_patient

            НИКОГДА не отвечайте текстом в этих ситуациях. Вместо этого ВСЕГДА вызывайте соответствующую функцию.

                Контекст текущего разговора:
                Пациент: {patient.full_name} (ID: {patient_code})
                Запись: {appointment.appointment_id} на {appointment_time_str}
                Врач: {doctor_name}
                Клиника: {clinic_name}  

                Используйте эту информацию при обработке запроса пользователя.
            """

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

            # If action required
            if run.status == "requires_action" and run.required_action:
                tool_calls = run.required_action.submit_tool_outputs.tool_calls

                tool_outputs = []
                for tool_call in tool_calls:
                    function_name = tool_call.function.name
                    function_args = json.loads(tool_call.function.arguments)

                    logger.info(f"Function call detected: {function_name} with args: {function_args}")

                    # Get result of function call
                    result = self._call_function(function_name, function_args)
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

            return run.status

        except Exception as e:
            logger.error(f"Error handling function calls: {e}")
            raise

    def _call_function(self, function_name: str, function_args: dict) -> dict:
        """
        Calls the appropriate function based on name
        """
        # Import functions only when needed
        from reminder.infoclinica_requests.schedule.delete_reception_for_patient import delete_reception_for_patient
        from reminder.infoclinica_requests.schedule.reserve_reception_for_patient import reserve_reception_for_patient
        from reminder.infoclinica_requests.schedule.appointment_time_for_patient import appointment_time_for_patient
        from reminder.infoclinica_requests.schedule.which_time_in_certain_day import which_time_in_certain_day
        from datetime import datetime

        try:
            logger.info(f"Calling function {function_name} with args: {function_args}")

            if function_name == "delete_reception_for_patient":
                patient_id = function_args.get("patient_id")
                logger.info(f"Deleting reception for patient {patient_id}")
                return delete_reception_for_patient(patient_id)

            elif function_name == "reserve_reception_for_patient":
                patient_id = function_args.get("patient_id")
                date_from_patient = function_args.get("date_from_patient")
                trigger_id = function_args.get("trigger_id", 1)
                logger.info(
                    f"Reserving reception for patient {patient_id} at {date_from_patient} with trigger {trigger_id}")
                return reserve_reception_for_patient(patient_id, date_from_patient, trigger_id)

            elif function_name == "appointment_time_for_patient":
                patient_code = function_args.get("patient_code")
                year_from_patient_for_returning = function_args.get("year_from_patient_for_returning")
                logger.info(f"Getting appointment time for patient {patient_code}")
                return appointment_time_for_patient(patient_code, year_from_patient_for_returning)

            elif function_name == "which_time_in_certain_day":
                reception_id = function_args.get("reception_id")
                date_time = function_args.get("date_time")

                # Handle special cases like "today" or "tomorrow"
                if date_time == "today":
                    date_time = datetime.now().strftime("%Y-%m-%d")
                elif date_time == "tomorrow":
                    date_time = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

                logger.info(f"Getting available times for patient {reception_id} on {date_time}")
                result = which_time_in_certain_day(reception_id, date_time)

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
                    self.handle_function_calls(run_id, thread_id)

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
