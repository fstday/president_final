import os
import django
import logging
import requests
import json
import xml.etree.ElementTree as ET
from datetime import datetime
from django.http import JsonResponse
from dotenv import load_dotenv

from reminder.infoclinica_requests.schedule.doct_schedule_free import check_day_has_free_slots
from reminder.infoclinica_requests.schedule.schedule_rec_reserve import schedule_rec_reserve
from reminder.infoclinica_requests.utils import compare_times_for_redis, compare_times, format_doctor_name
from reminder.models import Patient, Appointment, QueueInfo

# Настройка логирования и загрузка переменных окружения
logger = logging.getLogger(__name__)
load_dotenv()

infoclinica_api_url = os.getenv('INFOCLINICA_BASE_URL')
infoclinica_x_forwarded_host = os.getenv('INFOCLINICA_HOST')

# Пути к сертификатам
base_dir = os.path.dirname(os.path.abspath(__file__))
certs_dir = os.path.join(base_dir, 'certs')
os.makedirs(certs_dir, exist_ok=True)
cert_file_path = os.path.join(certs_dir, 'cert.pem')
key_file_path = os.path.join(certs_dir, 'key.pem')


def reserve_reception_for_patient(patient_id, date_from_patient, trigger_id):
    """
    Запись/перенос приема пациента с предварительной проверкой наличия свободных окон.

    Args:
        patient_id (str/int): Код пациента
        date_from_patient (str): Дата и время в формате YYYY-MM-DD HH:MM
        trigger_id (int): Идентификатор типа операции
                          1 - создание/изменение записи
                          2 - проверка альтернативных времен
                          3 - получение всех доступных времен

    Returns:
        dict/JsonResponse: Результат операции с соответствующим статусом
    """
    from reminder.openai_assistant.api_views import get_date_relation

    logger.info(f"🚀 Starting reserve_reception_for_patient with patient_id={patient_id}, "
                f"date_from_patient={date_from_patient}, trigger_id={trigger_id}")

    doctor_id = None  # Initialize variable to avoid potential reference errors
    target_filial_id = 1  # Default value

    try:
        # Извлекаем только дату из строки даты+времени
        date_parts = date_from_patient.split()
        date_only = date_parts[0] if len(date_parts) > 0 else None

        if date_only:
            # Предварительная проверка наличия свободных окон на указанную дату
            day_check = check_day_has_free_slots(patient_id, date_only)

            # Если на этот день нет свободных окон, сразу возвращаем результат
            if not day_check.get('has_slots', False):
                # Определяем отношение даты к текущему дню (сегодня/завтра)
                date_obj = datetime.strptime(date_only, '%Y-%m-%d')
                relation = get_date_relation(date_obj)

                # Используем существующие статусы ответов
                if relation == "today":
                    return {"status": "error_empty_windows_today",
                            "message": "Свободных приемов на сегодня не найдено.", "day": "сегодня", "day_kz": "бүгін"}
                elif relation == "tomorrow":
                    return {"status": "error_empty_windows_tomorrow",
                            "message": "Свободных приемов на завтра не найдено.", "day": "завтра", "day_kz": "ертең"}
                else:
                    return {"status": "error_empty_windows", "message": f"Свободных приемов на {date_only} не найдено."}

        # Продолжаем стандартную логику, когда слоты доступны
        try:
            found_patient = Patient.objects.get(patient_code=patient_id)
            # Look for active appointment
            existing_appointment = Appointment.objects.filter(
                patient=found_patient,
                is_active=True,
                is_infoclinica_id=True  # If this is a record from Infoclinica
            ).first()
            is_reschedule = existing_appointment is not None
            schedid = existing_appointment.appointment_id if is_reschedule else None

            # Find the latest appointment for this patient
            latest_appointment = Appointment.objects.filter(
                patient=found_patient,
                is_active=True
            ).order_by('-created_at').first()

            if latest_appointment:
                # Get doctor code from Doctor model
                if latest_appointment.doctor:
                    doctor_id = latest_appointment.doctor.doctor_code
                    print(f"Doctor ID from appointment: {doctor_id}")
                else:
                    # If no doctor in Appointment model, try to get from QueueInfo
                    latest_queue = QueueInfo.objects.filter(
                        patient=found_patient
                    ).order_by('-created_at').first()

                    if latest_queue and latest_queue.doctor_code:
                        doctor_id = latest_queue.doctor_code
                        print(f"Doctor ID from queue: {doctor_id}")
                    else:
                        print("⚠️ No doctor_code found")
                        return {
                            'status': 'error',
                            'message': 'Doctor code not found for this patient'
                        }

                # Get target clinic from Appointment model
                if latest_appointment.clinic:
                    target_filial_id = latest_appointment.clinic.clinic_id
                    print(f"Target clinic ID from appointment: {target_filial_id}")
                else:
                    # If no clinic in Appointment model, try to get from QueueInfo
                    latest_queue = QueueInfo.objects.filter(
                        patient=found_patient
                    ).order_by('-created_at').first()

                    if latest_queue and latest_queue.target_branch:
                        target_filial_id = latest_queue.target_branch.clinic_id
                        print(f"Target clinic ID from queue: {target_filial_id}")
                    else:
                        target_filial_id = 1  # Default value
                        print(f"Using default target clinic ID: {target_filial_id}")
            else:
                # If no appointments in Appointment model, try to find from QueueInfo
                latest_queue = QueueInfo.objects.filter(
                    patient=found_patient
                ).order_by('-created_at').first()

                if latest_queue:
                    if latest_queue.doctor_code:
                        doctor_id = latest_queue.doctor_code
                        print(f"Doctor ID from queue: {doctor_id}")
                    else:
                        print("⚠️ No doctor_code found in queue")
                        return {
                            'status': 'error',
                            'message': 'No doctor code found for this patient'
                        }

                    if latest_queue.target_branch:
                        target_filial_id = latest_queue.target_branch.clinic_id
                        print(f"Target clinic ID from queue: {target_filial_id}")
                    else:
                        target_filial_id = 1  # Default value
                        print(f"Using default target clinic ID: {target_filial_id}")
                else:
                    print("⚠️ No appointments or queue entries found for this patient")
                    return {
                        'status': 'error',
                        'message': 'No records found for this patient'
                    }

            # Final check for doctor_id
            if not doctor_id:
                return {
                    'status': 'error',
                    'message': 'Unable to determine doctor ID'
                }

        except Patient.DoesNotExist:
            print(f"❌ Patient with ID {patient_id} not found")
            return {"status": "error", "message": f"Patient with ID {patient_id} not found"}
        except Exception as e:
            print(f"❌ Exception occurred: {str(e)}")
            return {"status": "error", "message": f"Error: {str(e)}"}

        # Parse dates for XML request
        if isinstance(date_from_patient, str):
            try:
                date_part, time_part = date_from_patient.split()
                year, month, day = map(int, date_part.split('-'))
                hour, minute = map(int, time_part.split(':'))
                date_obj = datetime(year, month, day, hour, minute)
            except ValueError as e:
                return {"status": "error", "message": f"Invalid date format: {str(e)}"}
        elif isinstance(date_from_patient, datetime):
            date_obj = date_from_patient
            date_part = date_obj.strftime('%Y-%m-%d')
            time_part = date_obj.strftime('%H:%M')
        else:
            return {"status": "error", "message": "Invalid date type"}

        beginning_formatted_date = date_obj.strftime('%Y%m%d')
        time_obj = date_obj.time()

        logger.info(f'formatted_date: {beginning_formatted_date}')
        logger.info(f'date_part: {date_part}, time_part: {time_part}')

        if found_patient:
            logger.info(f'Найдена запись ID: {found_patient}')

            # Заголовки запроса
            headers = {
                'X-Forwarded-Host': f'{infoclinica_x_forwarded_host}',
                'Content-Type': 'text/xml'
            }

            # XML запрос для получения информации о свободных слотах для записи
            xml_request = f'''
            <WEB_SCHEDULE xmlns="http://sdsys.ru/">
              <MSH>
                <MSH.3></MSH.3>
                <MSH.7>
                  <TS.1>{datetime.now().strftime('%Y%m%d%H%M')}</TS.1>
                </MSH.7>
                <MSH.9>
                  <MSG.1>WEB</MSG.1>
                  <MSG.2>SCHEDULE</MSG.2>
                </MSH.9>
                <MSH.10>f2e89dbc1e813cb680d2f847</MSH.10>
                <MSH.18>UTF-8</MSH.18>
                <MSH.99>{target_filial_id}</MSH.99>
              </MSH>
              <SCHEDULE_IN>
                <INDOCTLIST>{doctor_id}</INDOCTLIST>
                <BDATE>{beginning_formatted_date}</BDATE>
                <FDATE>{beginning_formatted_date}</FDATE>
                <EXTINTERV>30</EXTINTERV> <!-- только для переноса! -->
                <SCHLIST/>
              </SCHEDULE_IN>
            </WEB_SCHEDULE>
            '''

            # Выполнение POST-запроса
            response = requests.post(
                url=infoclinica_api_url,
                headers=headers,
                data=xml_request,
                cert=(cert_file_path, key_file_path)
            )

            # Проверка на ошибки и вывод ответа сервера
            if response.status_code == 200:
                root = ET.fromstring(response.text)
                namespace = {'ns': 'http://sdsys.ru/'}

                free_time_intervals = []
                schedint = root.find('.//ns:SCHEDINT', namespace)
                schedident_text = schedint.find('ns:SCHEDIDENT', namespace).text if schedint is not None else None

                for interval in root.findall('.//ns:INTERVAL', namespace):
                    bhour = interval.find('ns:BHOUR', namespace).text
                    bmin = interval.find('ns:BMIN', namespace).text
                    fhour = interval.find('ns:FHOUR', namespace).text
                    fmin = interval.find('ns:FMIN', namespace).text
                    freetype = interval.find('ns:FREETYPE', namespace).text

                    # Выводим данные перед добавлением в список
                    logger.info(
                        f"Parsed interval: BHOUR={bhour}, BMIN={bmin}, FHOUR={fhour}, FMIN={fmin}, FREETYPE={freetype}")

                    if freetype == '1':  # Только свободные интервалы
                        start_time = f"{bhour}:{bmin.zfill(2)}"
                        end_time = f"{fhour}:{fmin.zfill(2)}"
                        free_time_intervals.append({"start_time": start_time, "end_time": end_time})

                logger.info("Свободные интервалы в формате JSON:")
                logger.info(free_time_intervals)

                # Обработка для trigger_id == 2
                if trigger_id == 2:
                    # Получаем все доступные времена вместо только 3 ближайших
                    result_times = compare_times_for_redis(free_time_intervals, time_obj, date_part)

                    answer = {
                        'status': 'suggest_times',
                        'suggested_times': result_times,
                        'message': f'Данное время {date_from_patient} было занято. Возвращаем все свободные времена',
                        'action': 'reserve',
                        'specialist_name': format_doctor_name(patient_id)
                    }

                    logger.info(answer)
                    return answer

                # Обработка для trigger_id == 1
                elif trigger_id == 1:
                    # Проверяем совпадение с запрошенным временем или получаем все доступные времена
                    result_time = compare_times(free_time_intervals, time_obj, date_part)

                    # Если result_time - список (все доступные времена)
                    if isinstance(result_time, list):
                        # Проверяем, есть ли точное совпадение с запрошенным временем
                        exact_match = f"{date_part} {time_obj.strftime('%H:%M')}"
                        if exact_match in result_time:
                            # Если есть точное совпадение, используем его
                            logger.info(f'Found exact match for requested time: {exact_match}')
                            return schedule_rec_reserve(
                                result_time=exact_match,
                                doctor_id=doctor_id,
                                date_part=date_part,
                                patient_id=patient_id,
                                date_obj=date_obj,
                                schedident_text=schedident_text,
                                free_intervals=free_time_intervals,
                                is_reschedule=is_reschedule,
                                schedid=schedid
                            )
                        else:
                            # Если точного совпадения нет, возвращаем все доступные времена
                            answer = {
                                'status': 'suggest_times',
                                'suggested_times': result_time,
                                'message': f'Данное время {date_from_patient} было занято. Возвращаем все свободные времена',
                                'action': 'reserve',
                                'specialist_name': format_doctor_name(patient_id)
                            }

                            logger.info(answer)
                            return answer
                    # Если result_time - строка (точное совпадение найдено)
                    elif result_time:
                        logger.info(f'Found suitable time {result_time}')

                        return schedule_rec_reserve(
                            result_time=result_time,
                            doctor_id=doctor_id,
                            date_part=date_part,
                            patient_id=patient_id,
                            date_obj=date_obj,
                            schedident_text=schedident_text,
                            free_intervals=free_time_intervals,
                            is_reschedule=is_reschedule,
                            schedid=schedid
                        )
                    else:
                        logger.info('Подходящее время не найдено')
                        answer = {
                            'message': f'Подходящее время не найдено'
                        }
                        return answer

                # Обработка для trigger_id == 3 (когда нужно на конкретный день узнать доступные записи)
                elif trigger_id == 3:
                    # Для этого режима просто возвращаем все доступные времена в формате списка
                    # В формате: ['2025-03-19 09:30', '2025-03-19 10:00', ...]
                    result_times = []
                    for interval in free_time_intervals:
                        start_time = interval["start_time"]
                        time_hour, time_min = map(int, start_time.split(':'))
                        # Пропускаем интервалы до 9:00 и после 21:00
                        if (time_hour < 9) or (time_hour >= 21):
                            continue
                        # Формируем строку времени с датой
                        formatted_time = f"{date_part} {start_time}"
                        result_times.append(formatted_time)

                    logger.info(f"Доступные времена для триггера 3: {result_times}")
                    return result_times
            else:
                logger.info('Ошибка при запросе:', response.status_code)
                logger.info('Ответ сервера:', response.text)
                return {
                    'status': 'error',
                    'message': f'Ошибка при запросе: {response.status_code}'
                }

        else:
            logger.info('Запись не найдена')
            return {
                'status': 'error',
                'message': 'Запись не найдена'
            }

    except Exception as e:
        logger.error(f"Ошибка в reserve_reception_for_patient: {e}", exc_info=True)
        return {"status": "error_med_element", "message": str(e)}
