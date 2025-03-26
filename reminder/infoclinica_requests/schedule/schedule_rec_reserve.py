from dotenv import load_dotenv
import requests
import os
import django
import logging
import xml.etree.ElementTree as ET
import redis
from datetime import datetime
from django.utils import timezone
from reminder.models import *
from reminder.infoclinica_requests.utils import compare_times_for_redis, compare_times, redis_reception_appointment, \
    compare_and_suggest_times

load_dotenv()
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'president_final.settings')
django.setup()

logger = logging.getLogger(__name__)
redis_client = redis.StrictRedis(host='localhost', port=6379, db=0)

infoclinica_api_url = os.getenv('INFOCLINICA_BASE_URL')
infoclinica_x_forwarded_host = os.getenv('INFOCLINICA_HOST')

# Paths to certificates
base_dir = os.path.dirname(os.path.abspath(__file__))
certs_dir = os.path.join(base_dir, 'certs')
os.makedirs(certs_dir, exist_ok=True)
cert_file_path = os.path.join(certs_dir, 'cert.pem')
key_file_path = os.path.join(certs_dir, 'key.pem')
current_date_time_for_xml = datetime.now().strftime('%Y%m%d%H%M%S')


def schedule_rec_reserve(result_time, doctor_id, date_part, patient_id, date_obj, schedident_text, free_intervals,
                         is_reschedule=False, schedid=None):
    """
    Функция резервирует время при его успешном нахождении в свободных окошках.
    Отправляет XML запрос и резервирует свободное время за клиентом. Метод: WEB_SCHEDULE_REC_RESERVE
    Обновлена для работы с полным списком доступных времен.

    :param result_time: Выбранное время для записи
    :param doctor_id: ID врача
    :param date_part: Дата записи
    :param patient_id: ID пациента
    :param date_obj: Объект datetime
    :param schedident_text: ID графика работы
    :param free_intervals: Список свободных интервалов
    :param is_reschedule: Флаг, указывающий, является ли это переносом записи
    :param schedid: ID существующей записи (только для переноса)
    :return: Словарь с результатом операции
    """

    try:
        # Получаем информацию о пациенте
        found_patient = Patient.objects.get(patient_code=patient_id)

        # Получаем информацию о филиале через очередь пациента
        latest_queue = found_patient.queue_entries.order_by('-created_at').first()

        # Инициализируем ID филиала значением по умолчанию
        filial_id = 1

        # Если очередь существует, получаем ID филиала из ветви
        if latest_queue and latest_queue.branch:
            filial_id = latest_queue.branch.clinic_id

        # Также проверяем, есть ли у пациента активная запись с информацией о клинике
        latest_appointment = Appointment.objects.filter(
            patient=found_patient,
            is_active=True
        ).order_by('-created_at').first()

        if latest_appointment and latest_appointment.clinic:
            filial_id = latest_appointment.clinic.clinic_id

        # Найти доктора по doctor_id
        doctor = None
        try:
            doctor = Doctor.objects.get(doctor_code=doctor_id)
            logger.info(f"Найден врач: {doctor.full_name}")
        except Doctor.DoesNotExist:
            logger.warning(f"Врач с кодом {doctor_id} не найден")

        # Найти клинику по filial_id
        clinic = None
        try:
            clinic = Clinic.objects.get(clinic_id=filial_id)
            logger.info(f"Найдена клиника: {clinic.name}")
        except Clinic.DoesNotExist:
            logger.warning(f"Клиника с ID {filial_id} не найдена")

        # Получить причину и отделение из QueueInfo, если возможно
        reason = None
        department = None

        if latest_queue:
            # Получаем причину из очереди
            reason = latest_queue.reason
            if reason:
                logger.info(f"Получена причина из очереди: {reason.reason_name}")

            # Если у доктора есть отделение, используем его
            if doctor and doctor.department:
                department = doctor.department
                logger.info(f"Получено отделение от врача: {department.name}")
            # Иначе проверяем инфо о департаменте в очереди
            elif latest_queue.department_number:
                try:
                    department = Department.objects.filter(department_id=latest_queue.department_number).first()
                    if department:
                        logger.info(f"Получено отделение из очереди: {department.name}")
                except Exception as e:
                    logger.warning(f"Ошибка при получении отделения: {str(e)}")

    except Patient.DoesNotExist:
        return {'status': 'error', 'message': 'Пациент не найден.'}

    headers = {
        'X-Forwarded-Host': f'{infoclinica_x_forwarded_host}',
        'Content-Type': 'text/xml'
    }

    logger.info(f'DATE OBJECT {date_obj}')
    if isinstance(result_time, str):
        try:
            result_time = datetime.strptime(result_time, '%Y-%m-%d %H:%M')
        except ValueError:
            return {'status': 'error', 'message': 'Неверный формат даты. Ожидаемый формат: YYYY-MM-DD HH:MM.'}

    bhour, bmin = result_time.hour, result_time.minute
    logger.info(f"Время для резервирования: {bhour}:{bmin}")

    workdate = date_part.replace('-', '')  # Преобразуем в формат без '-' для отправки XML запроса

    # Устанавливаем FHOUR и FMIN на 30 минут позже
    fmin = bmin + 30
    fhour = bhour

    if fmin >= 60:
        fmin -= 60
        fhour += 1

    # Убедимся, что часы находятся в диапазоне [0, 23]
    if fhour >= 24:
        fhour -= 24

    # Форматируем часы и минуты для BHOUR и BMIN
    bhour_str = str(bhour).zfill(2)
    bmin_str = str(bmin).zfill(2)
    fhour_str = str(fhour).zfill(2)
    fmin_str = str(fmin).zfill(2)

    # Создаем базовый XML-запрос
    xml_schedule_reserve_in = f"""
        <DCODE>{doctor_id}</DCODE>
        <WORKDATE>{workdate}</WORKDATE>
        <BHOUR>{bhour_str}</BHOUR>
        <BMIN>{bmin_str}</BMIN>
        <FHOUR>{fhour_str}</FHOUR>
        <FMIN>{fmin_str}</FMIN>
        <SCHEDIDENT>{schedident_text}</SCHEDIDENT>
        <PCODE>{patient_id}</PCODE>
        <ONLINETYPE>0</ONLINETYPE>
        <SCHLIST/>
    """

    # Добавляем SCHEDID только если это перенос записи
    if is_reschedule and schedid:
        xml_schedule_reserve_in = f"""
        <DCODE>{doctor_id}</DCODE>
        <WORKDATE>{workdate}</WORKDATE>
        <BHOUR>{bhour_str}</BHOUR>
        <BMIN>{bmin_str}</BMIN>
        <FHOUR>{fhour_str}</FHOUR>
        <FMIN>{fmin_str}</FMIN>
        <SCHEDIDENT>{schedident_text}</SCHEDIDENT>
        <PCODE>{patient_id}</PCODE>
        <SCHEDID>{schedid}</SCHEDID>
        <ONLINETYPE>0</ONLINETYPE>
        <SCHLIST/>
        """

    # Финальный XML-запрос
    xml_request = f"""
    <WEB_SCHEDULE_REC_RESERVE xmlns="http://sdsys.ru/">
      <MSH>
        <MSH.3></MSH.3>
        <MSH.7>
          <TS.1>{current_date_time_for_xml}</TS.1>
        </MSH.7>
        <MSH.9>
          <MSG.1>WEB</MSG.1>
          <MSG.2>SCHEDULE_REC_RESERVE</MSG.2>
        </MSH.9>
        <MSH.10>f2e89dbc1e813cb680d2f847</MSH.10>
        <MSH.18>UTF-8</MSH.18>
        <MSH.99>{filial_id}</MSH.99>
      </MSH>
      <SCHEDULE_REC_RESERVE_IN>
        {xml_schedule_reserve_in}
      </SCHEDULE_REC_RESERVE_IN>
    </WEB_SCHEDULE_REC_RESERVE>
    """

    logger.info(
        f"Отправляем запрос на резервирование: DCODE={doctor_id}, WORKDATE={workdate}, BHOUR={bhour_str}, BMIN={bmin_str}")

    try:
        response = requests.post(
            url=infoclinica_api_url,
            headers=headers,
            data=xml_request,
            cert=(cert_file_path, key_file_path)
        )

        if response.status_code == 200:
            try:
                logger.debug(f"Тело ответа: {response.text[:500]}...")  # Логируем первые 500 символов ответа
                root = ET.fromstring(response.text)
                namespace = {'ns': 'http://sdsys.ru/'}

                sp_result_code = root.find('.//ns:SPRESULT', namespace)
                sp_comment_text = root.find('.//ns:SPCOMMENT', namespace)

                sp_result = int(sp_result_code.text) if sp_result_code is not None else None

                if sp_result is not None:
                    if sp_result == 1:
                        schedid_element = root.find(".//ns:SCHEDID", namespace)
                        if schedid_element is not None and schedid_element.text:
                            schedid_value = int(schedid_element.text)

                            # Создаем или обновляем запись на прием СО ВСЕМИ СВЯЗЯМИ
                            appointment, created = Appointment.objects.update_or_create(
                                appointment_id=schedid_value,
                                defaults={
                                    'patient': found_patient,
                                    'doctor': doctor,
                                    'clinic': clinic,
                                    'department': department,
                                    'reason': reason,
                                    'is_infoclinica_id': True,
                                    'start_time': date_obj,
                                    'end_time': date_obj + timezone.timedelta(minutes=30),
                                    'is_active': True
                                }
                            )
                            logger.info(f"{'Создана' if created else 'Обновлена'} запись на прием: {appointment}")

                        # Обновляем данные, если это перенос
                        if is_reschedule and schedid:
                            try:
                                # Поиск существующей записи
                                old_appointment = Appointment.objects.get(appointment_id=schedid)

                                # Удаление старого времени из Redis
                                previous_appointment_time_str = old_appointment.start_time.strftime('%Y-%m-%d %H:%M:%S')
                                redis_client.delete(previous_appointment_time_str)

                                # Обновляем время и дату
                                old_appointment.start_time = date_obj
                                old_appointment.end_time = date_obj + timezone.timedelta(minutes=30)
                                old_appointment.save()
                            except Appointment.DoesNotExist:
                                logger.warning(f"Запись с ID {schedid} не найдена в Appointment")

                        # Заносим время и дату в Redis
                        appointment_time_str = date_obj.strftime('%Y-%m-%d %H:%M:%S')
                        redis_reception_appointment(patient_id=patient_id, appointment_time=appointment_time_str)

                        return {
                            'status': 'success_schedule',
                            'message': f'Запись произведена успешно на {appointment_time_str}',
                            'time': appointment_time_str
                        }

                    if sp_result == 0:
                        logger.info('К сожалению, выбранное время было недавно занято.')

                        # Возвращаем все доступные времена
                        available_times = compare_and_suggest_times(free_intervals, result_time.time(), date_part)

                        answer = {
                            'status': 'suggest_times',
                            'suggested_times': available_times,
                            'message': f'Время приема {result_time} занято. Возвращаю все свободные времена для записи',
                            'action': 'reserve'
                        }

                        logger.info(f"Возвращаю из schedule_rec_reserve: {answer}")
                        return answer

                    else:
                        logger.info('Пришел неверный код')
                        return {
                            'status': 'incorrect_code',
                            'message': 'Пришел неверный код'
                        }

            except ET.ParseError as e:
                logger.info(f'Ошибка при парсинге XML: {e}')
                return {
                    'status': 'error',
                    'message': f'Ошибка при парсинге XML: {e}'
                }

    except requests.exceptions.RequestException as e:
        logger.info(f'Ошибка при выполнении запроса: {e}')
        return {
            'status': 'error',
            'message': f'Ошибка при выполнении запроса: {e}'
        }

    # Если код доходит до сюда, значит, произошла ошибка
    return {
        'status': 'error',
        'message': 'Не удалось завершить операцию резервирования.'
    }