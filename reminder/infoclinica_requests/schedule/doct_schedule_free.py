import os
import django

# Настройки Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'president_final.settings')
django.setup()

import logging
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from reminder.infoclinica_requests.schedule.schedule_cache import (
    get_cached_schedule, cache_schedule, check_day_has_slots_from_cache
)

# Настройка логирования
logger = logging.getLogger(__name__)

# Импорты моделей
from reminder.models import Clinic, Doctor, Department, Patient, QueueInfo, PatientDoctorAssociation
from reminder.infoclinica_requests.utils import generate_msh_10

# Загрузка переменных окружения
from dotenv import load_dotenv

load_dotenv()
infoclinica_api_url = os.getenv('INFOCLINICA_BASE_URL')
infoclinica_x_forwarded_host = os.getenv('INFOCLINICA_HOST')

# Пути к сертификатам
base_dir = os.path.dirname(os.path.abspath(__file__))
certs_dir = os.path.join(base_dir, 'certs')
os.makedirs(certs_dir, exist_ok=True)
cert_file_path = os.path.join(certs_dir, 'cert.pem')
key_file_path = os.path.join(certs_dir, 'key.pem')


def get_patient_doctor_schedule(patient_code, days_horizon=7, online_mode=0, return_raw=False):
    """
    Получение графика работы врача для пациента с использованием запроса DOCT_SCHEDULE_FREE.
    """
    logger.info(f"Запрос графика работы для пациента: {patient_code}")

    # Получаем параметры врача, отделения и клиники для пациента
    doctor_code, department_id, clinic_id = get_available_doctor_by_patient(patient_code)

    logger.info(f"Для пациента {patient_code} найдены следующие параметры:")
    logger.info(f"- Врач: {doctor_code}")
    logger.info(f"- Отделение: {department_id}")
    logger.info(f"- Филиал: {clinic_id}")

    # Проверка, что хотя бы один параметр для фильтрации указан
    if not doctor_code and not department_id and not clinic_id:
        logger.error("❌ Не удалось найти необходимые параметры для запроса графика")
        if return_raw:
            return None
        return {
            'success': False,
            'error': "Не удалось найти необходимые параметры для запроса графика"
        }

    try:
        # Временные параметры для запроса
        current_date = datetime.now()
        end_date = current_date + timedelta(days=days_horizon)

        # Форматируем даты для запроса
        bdate = current_date.strftime("%Y%m%d")
        fdate = end_date.strftime("%Y%m%d")

        # Генерируем уникальный ID сообщения и текущую метку времени
        ts_1 = datetime.now().strftime("%Y%m%d%H%M%S")
        msh_10 = generate_msh_10()

        # Формируем элементы запроса в зависимости от имеющихся данных
        filial_element = f"<FILIALLIST>{clinic_id}</FILIALLIST>" if clinic_id else "<FILIALLIST></FILIALLIST>"

        # Если есть doctor_code - отправляем только его
        if doctor_code:
            doctor_filter = f"<DOCTLIST>{doctor_code}</DOCTLIST>"
            department_filter = ""
        # Если есть только department_id - отправляем только его
        elif department_id:
            doctor_filter = ""
            department_filter = f"<CASHLIST>{department_id}</CASHLIST>"
        else:
            doctor_filter = ""
            department_filter = ""

        # Construct the XML request
        xml_request = f'''
        <WEB_DOCT_SCHEDULE_FREE xmlns="http://sdsys.ru/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
          <MSH>
            <MSH.7>
              <TS.1>{ts_1}</TS.1>
            </MSH.7>
            <MSH.9>
              <MSG.1>WEB</MSG.1>
              <MSG.2>DOCT_SCHEDULE_FREE</MSG.2>
            </MSH.9>
            <MSH.10>{msh_10}</MSH.10>
            <MSH.18>UTF-8</MSH.18>
            <MSH.99>{clinic_id if clinic_id else ""}</MSH.99>
          </MSH>
          <DOCT_SCHEDULE_FREE_IN>
            {filial_element}{department_filter}{doctor_filter}
            <SCHEDIDENTLIST></SCHEDIDENTLIST>
            <BDATE>{bdate}</BDATE>
            <FDATE>{fdate}</FDATE>
            <ONLINEMODE>{online_mode}</ONLINEMODE>
          </DOCT_SCHEDULE_FREE_IN>
        </WEB_DOCT_SCHEDULE_FREE>
        '''

        logger.info(f"Отправка запроса DOCT_SCHEDULE_FREE:\n{xml_request}")

        # Выполняем HTTP-запрос
        response = requests.post(
            url=infoclinica_api_url,
            headers={
                'X-Forwarded-Host': infoclinica_x_forwarded_host,
                'Content-Type': 'text/xml'
            },
            data=xml_request,
            cert=(cert_file_path, key_file_path),
            verify=True
        )

        # Проверяем ответ сервера
        if response.status_code == 200:
            logger.info(f"Получен ответ от сервера (длина: {len(response.text)} символов)")

            logger.info(f"Полный ответ DOCT_SCHEDULE_FREE: \n{response.text[:1000]}...")
            if len(response.text) > 1000:
                logger.info(f"... (оставшаяся часть ответа из {len(response.text)} символов)")

            # Если запрошен сырой ответ, возвращаем его
            if return_raw:
                return response.text

            # Иначе разбираем XML-ответ и возвращаем результат
            return parse_doctor_schedule_response(response.text)
        else:
            logger.error(f"❌ Ошибка запроса: {response.status_code}")
            logger.error(f"Ответ сервера: {response.text}")

            if return_raw:
                return response.text

            return {
                'success': False,
                'error': f"Ошибка HTTP-запроса: {response.status_code}"
            }

    except Exception as e:
        logger.error(f"❌ Исключение при получении графика работы: {e}", exc_info=True)

        if return_raw:
            return None

        return {
            'success': False,
            'error': f"Исключение: {str(e)}"
        }


def get_available_doctor_by_patient(patient_code):
    """
    Определяет доступного врача для пациента с улучшенной поддержкой
    множественных врачей из одного отделения.

    Параметры:
    patient_code (int): Код пациента

    Возвращает:
    tuple: (doctor_code, department_id, clinic_id)
    """
    try:
        # Проверяем очереди пациента
        queues = QueueInfo.objects.filter(patient__patient_code=patient_code).order_by('-created_at')

        if queues.exists():
            latest_queue = queues.first()

            # Приоритеты:
            # 1. Врач + отделение + филиал из очереди
            if latest_queue.doctor_code and latest_queue.department_number and latest_queue.target_branch:
                logger.info(f"Найден врач {latest_queue.doctor_code} в очереди пациента {patient_code}")
                return (
                    latest_queue.doctor_code,
                    latest_queue.department_number,
                    latest_queue.target_branch.clinic_id
                )

            # 2. Врач + филиал
            if latest_queue.doctor_code and latest_queue.target_branch:
                logger.info(f"Найден врач {latest_queue.doctor_code} и филиал в очереди пациента {patient_code}")
                return (
                    latest_queue.doctor_code,
                    None,
                    latest_queue.target_branch.clinic_id
                )

            # 3. Отделение + филиал
            if latest_queue.department_number and latest_queue.target_branch:
                logger.info(f"Найдено отделение {latest_queue.department_number} в очереди пациента {patient_code}")
                return (
                    None,
                    latest_queue.department_number,
                    latest_queue.target_branch.clinic_id
                )

            # 4. Только филиал
            if latest_queue.target_branch:
                logger.info(f"Найден только филиал в очереди пациента {patient_code}")
                return (None, None, latest_queue.target_branch.clinic_id)

        # Если не нашли в очередях, проверяем пациента напрямую
        patient = Patient.objects.filter(patient_code=patient_code).first()
        if patient:
            # Проверяем сначала последнего использованного врача
            if patient.last_used_doctor:
                logger.info(f"Используем последнего врача пациента: {patient.last_used_doctor.doctor_code}")
                # Находим клинику через записи
                latest_appointment = patient.appointments.filter(
                    doctor=patient.last_used_doctor,
                    is_active=True
                ).order_by('-start_time').first()

                clinic_id = latest_appointment.clinic.clinic_id if latest_appointment and latest_appointment.clinic else 1
                department_id = None
                if latest_appointment and latest_appointment.department:
                    department_id = latest_appointment.department.department_id

                return (
                    patient.last_used_doctor.doctor_code,
                    department_id,
                    clinic_id
                )

            # Проверяем активные записи пациента
            appointments = patient.appointments.filter(is_active=True).order_by('-start_time')
            if appointments.exists():
                latest_appointment = appointments.first()

                # Приоритеты как в случае с очередями
                if latest_appointment.doctor and latest_appointment.department and latest_appointment.clinic:
                    logger.info(f"Найден врач {latest_appointment.doctor.doctor_code} в записи пациента {patient_code}")
                    return (
                        latest_appointment.doctor.doctor_code,
                        latest_appointment.department.department_id,
                        latest_appointment.clinic.clinic_id
                    )

                if latest_appointment.doctor and latest_appointment.clinic:
                    logger.info(f"Найден врач {latest_appointment.doctor.doctor_code} в записи пациента {patient_code}")
                    return (
                        latest_appointment.doctor.doctor_code,
                        None,
                        latest_appointment.clinic.clinic_id
                    )

                if latest_appointment.department and latest_appointment.clinic:
                    logger.info(
                        f"Найдено отделение {latest_appointment.department.department_id} в записи пациента {patient_code}")
                    return (
                        None,
                        latest_appointment.department.department_id,
                        latest_appointment.clinic.clinic_id
                    )

                if latest_appointment.clinic:
                    logger.info(f"Найден только филиал в записи пациента {patient_code}")
                    return (None, None, latest_appointment.clinic.clinic_id)

            # Проверяем ассоциации пациента с врачом
            associations = PatientDoctorAssociation.objects.filter(patient=patient).order_by('-last_booking_date')
            if associations.exists():
                latest_association = associations.first()
                if latest_association.doctor:
                    logger.info(
                        f"Найден врач {latest_association.doctor.doctor_code} в ассоциациях пациента {patient_code}")
                    # Для клиники и отделения ищем информацию из записей
                    latest_appointment = patient.appointments.filter(
                        doctor=latest_association.doctor
                    ).order_by('-start_time').first()

                    clinic_id = 1  # Значение по умолчанию
                    department_id = None

                    if latest_appointment:
                        if latest_appointment.clinic:
                            clinic_id = latest_appointment.clinic.clinic_id
                        if latest_appointment.department:
                            department_id = latest_appointment.department.department_id

                    return (
                        latest_association.doctor.doctor_code,
                        department_id,
                        clinic_id
                    )

    except Exception as e:
        logger.error(f"❌ Ошибка при определении врача для пациента: {e}", exc_info=True)

    # Если ничего не нашли, возвращаем None для всех значений
    logger.warning(f"Не найдено информации о враче для пациента {patient_code}")
    return (None, None, 1)  # Возвращаем 1 как филиал по умолчанию


def select_best_doctor_from_schedules(schedules_by_doctor, target_date_str, target_time_str=None):
    """
    Выбирает лучшего врача из доступных с расписанием на указанную дату и время.
    Учитывает блокирующие ограничения (CHECKCODE=2) и выбирает врачей без них в приоритете.

    Args:
        schedules_by_doctor: Словарь расписаний по врачам
        target_date_str: Целевая дата в формате YYYY-MM-DD
        target_time_str: Целевое время в формате HH:MM (опционально)

    Returns:
        tuple: (doctor_code, doctor_name, department_id, schedule_id)
    """
    best_doctor = None
    most_free_slots = 0
    best_schedule_id = None

    # Если указано целевое время, конвертируем его в минуты от начала дня для сравнения
    target_minutes = None
    if target_time_str:
        try:
            if isinstance(target_time_str, str) and ':' in target_time_str:
                hours, minutes = map(int, target_time_str.split(':')[:2])
                target_minutes = hours * 60 + minutes
        except Exception as e:
            logger.warning(f"Не удалось разобрать время {target_time_str}: {e}")

    # Сортируем врачей по наличию блокирующих ограничений (CHECKCODE=2)
    doctors_without_blocking = {}
    doctors_with_blocking = {}

    for doctor_code, doctor_data in schedules_by_doctor.items():
        has_blocking = False

        # Проверяем наличие ограничений в расписаниях врача
        for schedule in doctor_data.get('schedules', []):
            if 'check_data' in schedule:
                check_data = schedule.get('check_data', [])
                if isinstance(check_data, list):
                    for check in check_data:
                        if check.get('check_code') == '2':
                            has_blocking = True
                            logger.warning(
                                f"Врач {doctor_code} ({doctor_data.get('doctor_name', '')}) имеет блокирующее ограничение (CHECKCODE=2): {check.get('check_label', '')}")
                            break
                elif isinstance(check_data, dict):
                    if check_data.get('check_code') == '2':
                        has_blocking = True
                        logger.warning(
                            f"Врач {doctor_code} ({doctor_data.get('doctor_name', '')}) имеет блокирующее ограничение (CHECKCODE=2): {check_data.get('check_label', '')}")

            if has_blocking:
                break

        # Распределяем врачей по группам
        if has_blocking:
            doctors_with_blocking[doctor_code] = doctor_data
        else:
            doctors_without_blocking[doctor_code] = doctor_data

    # Сперва пытаемся выбрать из врачей без блокирующих ограничений
    target_doctors = doctors_without_blocking if doctors_without_blocking else doctors_with_blocking

    logger.info(
        f"Найдено {len(doctors_without_blocking)} врачей без блокирующих ограничений и {len(doctors_with_blocking)} с блокирующими ограничениями")

    if not doctors_without_blocking and doctors_with_blocking:
        logger.warning("Все доступные врачи имеют блокирующие ограничения (CHECKCODE=2)! Запись может не сработать.")

    for doctor_code, doctor_data in target_doctors.items():
        free_slots_count = 0
        schedule_id = None
        closest_time_diff = float('inf')
        closest_schedule_id = None

        for schedule in doctor_data.get('schedules', []):
            if schedule.get('date_iso') == target_date_str and schedule.get('has_free_slots', False):
                # Подсчитываем свободные слоты
                this_slot_count = schedule.get('free_count', 0)
                free_slots_count += this_slot_count

                # Если не задано время, просто запоминаем schedident для первого или с наибольшим количеством слотов
                if not schedule_id and this_slot_count > 0:
                    schedule_id = schedule.get('schedule_id')

                # Если задано целевое время, ищем ближайший доступный слот по времени
                if target_minutes is not None and 'begin_hour' in schedule and 'begin_min' in schedule:
                    begin_hour = schedule.get('begin_hour', 0)
                    begin_min = schedule.get('begin_min', 0)
                    slot_minutes = begin_hour * 60 + begin_min

                    time_diff = abs(slot_minutes - target_minutes)
                    if time_diff < closest_time_diff:
                        closest_time_diff = time_diff
                        closest_schedule_id = schedule.get('schedule_id')

        # Если есть целевое время и нашли ближайший слот, используем его schedident
        if target_minutes is not None and closest_schedule_id:
            schedule_id = closest_schedule_id

        # Решаем, лучший ли это врач
        if free_slots_count > most_free_slots:
            most_free_slots = free_slots_count
            best_doctor = {
                'doctor_code': doctor_code,
                'doctor_name': doctor_data.get('doctor_name', ''),
                'department_id': doctor_data.get('department_id'),
                'schedule_id': schedule_id
            }

    if best_doctor:
        logger.info(
            f"Выбран врач: {best_doctor['doctor_name']} (ID: {best_doctor['doctor_code']}) с {most_free_slots} свободными слотами, schedident: {best_doctor['schedule_id']}")
        return best_doctor['doctor_code'], best_doctor['doctor_name'], best_doctor['department_id'], best_doctor[
            'schedule_id']

    return None, None, None, None


def check_day_has_free_slots(patient_code, date_str):
    """
    Проверяет, есть ли свободные слоты на конкретный день с использованием
    кэширования результатов на 15 минут для всей недели.

    Args:
        patient_code: Код пациента
        date_str: Дата в формате YYYY-MM-DD

    Returns:
        dict: Информация о доступности слотов
    """
    try:
        # Сначала проверяем кэш для всей недели
        cached_result = get_cached_schedule(patient_code)

        if cached_result:
            logger.info(f"Используем кэшированные данные для проверки доступности слотов на {date_str}")
            cache_check = check_day_has_slots_from_cache(patient_code, date_str, cached_result)

            # Если в кэше есть данные о доступности, возвращаем их
            if cache_check:
                return cache_check

        # Если кэша нет или он не содержит нужного дня, делаем запрос на всю неделю
        logger.info(f"Выполняем запрос DOCT_SCHEDULE_FREE для пациента {patient_code} на 7 дней")

        # Получаем расписание на всю неделю
        schedule_result = get_patient_doctor_schedule(patient_code, days_horizon=7)

        # Кэшируем результат для всех последующих запросов (на 15 минут)
        if schedule_result.get('success', False):
            cache_schedule(patient_code, schedule_result)
            logger.info(f"Результат запроса DOCT_SCHEDULE_FREE закэширован на 15 минут")

        # Проверяем наличие слотов на указанную дату
        if not schedule_result.get('success', False):
            logger.error(f"Ошибка при получении графика: {schedule_result.get('error', 'Неизвестная ошибка')}")
            return {
                'has_slots': False,
                'doctor_code': None,
                'department_id': None,
                'clinic_id': None
            }

        schedules = schedule_result.get('schedules', [])

        # Форматируем дату для поиска
        if date_str == "today":
            search_date = datetime.now().date()
        elif date_str == "tomorrow":
            search_date = (datetime.now() + timedelta(days=1)).date()
        else:
            search_date = datetime.strptime(date_str, "%Y-%m-%d").date()

        search_date_str = search_date.strftime("%Y-%m-%d")

        # Ищем слоты на указанную дату
        has_slots = False
        doctor_code = None
        department_id = None
        clinic_id = None

        for slot in schedules:
            slot_date = slot.get('date_iso')

            if slot_date == search_date_str and slot.get('has_free_slots', False):
                has_slots = True
                doctor_code = slot.get('doctor_code')
                department_id = slot.get('department_id')
                clinic_id = slot.get('clinic_id')
                break

        logger.info(f"Проверка доступности слотов на {date_str}: {'Доступны' if has_slots else 'Не доступны'}")

        return {
            'has_slots': has_slots,
            'doctor_code': doctor_code,
            'department_id': department_id,
            'clinic_id': clinic_id
        }

    except Exception as e:
        logger.error(f"Ошибка при проверке доступности слотов: {e}", exc_info=True)
        return {
            'has_slots': False,
            'doctor_code': None,
            'department_id': None,
            'clinic_id': None
        }


def parse_doctor_schedule_response(xml_response):
    """
    Разбор XML-ответа для запроса DOCT_SCHEDULE_FREE
    Обрабатывает как ответы с одним врачом, так и ответы со всеми врачами отделения
    Добавлена проверка ограничений по направлению

    Параметры:
    xml_response (str): XML ответ от сервера

    Возвращает:
    dict: Структурированный словарь с результатами
    """
    try:
        root = ET.fromstring(xml_response)
        namespace = {'ns': 'http://sdsys.ru/'}

        # Проверяем статус ответа
        msa_element = root.find(".//ns:MSA/ns:MSA.1", namespace)
        if msa_element is None or msa_element.text != "AA":
            logger.warning(
                f"❗ Неуспешный ответ от сервера: {msa_element.text if msa_element is not None else 'Не найден'}")

            # Проверяем, есть ли сообщение об ошибке
            err_element = root.find(".//ns:ERR/ns:ERR.3/ns:CWE.2", namespace)
            err_message = err_element.text if err_element is not None else "Неизвестная ошибка"

            sp_comment = root.find(".//ns:SPCOMMENT", namespace)
            if sp_comment is not None:
                err_message = sp_comment.text

            return {
                'success': False,
                'error': err_message
            }

        # Проверяем код результата
        sp_result = root.find(".//ns:SPRESULT", namespace)
        if sp_result is not None and sp_result.text != "1":
            sp_comment = root.find(".//ns:SPCOMMENT", namespace)
            err_message = sp_comment.text if sp_comment is not None else "Ошибка при получении расписания"

            return {
                'success': False,
                'error': err_message
            }

        # Ищем все интервалы графика в ответе
        schedule_elements = root.findall(".//ns:DOCT_SCHEDULE_FREE_OUT/ns:SCHINTERVAL", namespace)

        if not schedule_elements:
            logger.info("ℹ️ График работы не найден в ответе")
            return {
                'success': True,
                'schedules': [],
                'by_doctor': {}  # Добавляем структуру для группировки по врачам
            }

        schedules = []
        by_doctor = {}  # Группировка по врачам

        for sched in schedule_elements:
            schedule_data = {}

            # Базовые параметры графика
            field_mappings = {
                'schedule_id': 'SCHEDIDENT',
                'doctor_code': 'DCODE',
                'doctor_name': 'DNAME',
                'department_id': 'DEPNUM',
                'department_name': 'DEPNAME',
                'clinic_id': 'FILIAL',
                'clinic_name': 'FNAME',
                'timezone': 'TIMEZONE',
                'date': 'WDATE',
                'begin_hour': 'BEGHOUR',
                'begin_min': 'BEGMIN',
                'end_hour': 'ENDHOUR',
                'end_min': 'ENDMIN',
                'room_num': 'RNUM',
                'room_floor': 'RFLOOR',
                'room_building': 'RBUILDING',
                'free_flag': 'FREEFLAG',
                'free_count': 'FREECOUNT',
                'online_mode': 'ONLINEMODE'
            }

            for field_name, xml_field in field_mappings.items():
                element = sched.find(f"ns:{xml_field}", namespace)
                if element is not None and element.text:
                    # Преобразуем численные поля в int
                    numeric_fields = [
                        'schedule_id', 'doctor_code', 'department_id', 'clinic_id',
                        'begin_hour', 'begin_min', 'end_hour', 'end_min',
                        'free_flag', 'free_count', 'online_mode', 'timezone'
                    ]

                    if field_name in numeric_fields:
                        try:
                            schedule_data[field_name] = int(element.text)
                        except ValueError:
                            schedule_data[field_name] = element.text
                    else:
                        schedule_data[field_name] = element.text

            # Проверяем наличие ограничений (CHECKDATA)
            check_elements = sched.findall(".//ns:CHECKDATA", namespace)
            if check_elements:
                check_data = []
                for check in check_elements:
                    check_info = {}
                    check_code = check.find("ns:CHECKCODE", namespace)
                    check_label = check.find("ns:CHECKLABEL", namespace)
                    check_text = check.find("ns:CHECKTEXT", namespace)

                    if check_code is not None:
                        check_info['check_code'] = check_code.text
                    if check_label is not None:
                        check_info['check_label'] = check_label.text
                    if check_text is not None:
                        check_info['check_text'] = check_text.text

                    check_data.append(check_info)

                schedule_data['check_data'] = check_data

            # Форматируем дополнительные поля для удобства
            if 'date' in schedule_data:
                try:
                    date_str = schedule_data['date']
                    if len(date_str) == 8:  # Формат YYYYMMDD
                        year, month, day = int(date_str[0:4]), int(date_str[4:6]), int(date_str[6:8])

                        # Добавляем форматированные значения для удобства
                        schedule_data['date_formatted'] = f"{day:02d}.{month:02d}.{year}"
                        schedule_data[
                            'date_iso'] = f"{year}-{month:02d}-{day:02d}"  # Формат YYYY-MM-DD для совместимости

                        # Форматируем время начала и конца
                        if all(k in schedule_data for k in ['begin_hour', 'begin_min', 'end_hour', 'end_min']):
                            bh, bm = schedule_data['begin_hour'], schedule_data['begin_min']
                            eh, em = schedule_data['end_hour'], schedule_data['end_min']

                            schedule_data['begin_time'] = f"{bh:02d}:{bm:02d}"
                            schedule_data['end_time'] = f"{eh:02d}:{em:02d}"

                            # Расчет продолжительности в минутах
                            begin_minutes = bh * 60 + bm
                            end_minutes = eh * 60 + em
                            duration = end_minutes - begin_minutes
                            schedule_data['duration_minutes'] = duration
                except (ValueError, KeyError) as e:
                    logger.warning(f"❗ Ошибка при форматировании даты/времени: {e}")

            # Добавляем флаг "есть свободное время"
            if 'free_flag' in schedule_data and schedule_data['free_flag'] == 1:
                schedule_data['has_free_slots'] = True
            else:
                schedule_data['has_free_slots'] = False

            # Текстовое описание режима онлайн-приема
            if 'online_mode' in schedule_data:
                online_mode_map = {
                    0: "Прием в клинике",
                    1: "Прием в клинике или онлайн",
                    2: "Дежурный прием онлайн",
                    3: "Прием только онлайн"
                }
                schedule_data['online_mode_text'] = online_mode_map.get(
                    schedule_data['online_mode'], f"Неизвестный режим ({schedule_data['online_mode']})"
                )

            schedules.append(schedule_data)

            # Группируем по врачам
            doctor_code = schedule_data.get('doctor_code')
            if doctor_code:
                if doctor_code not in by_doctor:
                    by_doctor[doctor_code] = {
                        'doctor_name': schedule_data.get('doctor_name'),
                        'department_id': schedule_data.get('department_id'),
                        'department_name': schedule_data.get('department_name'),
                        'clinic_id': schedule_data.get('clinic_id'),
                        'clinic_name': schedule_data.get('clinic_name'),
                        'schedules': []
                    }
                by_doctor[doctor_code]['schedules'].append(schedule_data)

        # Сортируем результаты по дате и времени
        schedules.sort(key=lambda x: (x.get('date', ''), x.get('begin_hour', 0), x.get('begin_min', 0)))

        # Логирование для отладки
        if by_doctor:
            logger.info(f"Найдено врачей: {len(by_doctor)}")
            for doctor_code, doctor_data in by_doctor.items():
                logger.info(
                    f"Врач: {doctor_data['doctor_name']} (ID: {doctor_code}), расписаний: {len(doctor_data['schedules'])}")

        return {
            'success': True,
            'schedules': schedules,
            'by_doctor': by_doctor  # Включаем группировку по врачам
        }

    except Exception as e:
        logger.error(f"❌ Ошибка при разборе ответа: {e}", exc_info=True)
        return {
            'success': False,
            'error': f"Ошибка при разборе ответа: {str(e)}"
        }


# Пример использования
if __name__ == "__main__":
    # Пример с пациентом из реальных данных
    patient_code = 10000240  # Пример: Тест Иван Иванович

    # Сначала получаем сырой XML-ответ
    print("\nПОЛУЧЕНИЕ ГРАФИКА РАБОТЫ ДЛЯ ПАЦИЕНТА")
    raw_response = get_patient_doctor_schedule(patient_code, return_raw=True)

    # Сохраняем сырой ответ для печати позже
    print("\nСЫРОЙ XML-ОТВЕТ:")
    print("-" * 80)
    print(raw_response)
    print("-" * 80)

    # Затем используем сырой ответ для создания обработанного вывода
    processed_result = parse_doctor_schedule_response(raw_response)
