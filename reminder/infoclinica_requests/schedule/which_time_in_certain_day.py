import logging
import pytz
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, time as dt_time
from django.http import JsonResponse
from dotenv import load_dotenv
import os

from reminder.infoclinica_requests.schedule.doct_schedule_free import check_day_has_free_slots
from reminder.infoclinica_requests.schedule.schedule_cache import (
    get_cached_schedule, cache_schedule, check_day_has_slots_from_cache
)
from reminder.models import Patient, Appointment, QueueInfo, Clinic, AvailableTimeSlot, Doctor, PatientDoctorAssociation
from reminder.infoclinica_requests.utils import format_doctor_name, format_russian_date
from django.utils.timezone import make_aware

# Загрузка переменных окружения и настройка логирования
load_dotenv()
logger = logging.getLogger(__name__)

# Загрузка конфигурации из переменных окружения
infoclinica_api_url = os.getenv('INFOCLINICA_BASE_URL')
infoclinica_x_forwarded_host = os.getenv('INFOCLINICA_HOST')

# Paths to certificates
base_dir = os.path.dirname(os.path.abspath(__file__))
certs_dir = os.path.join(base_dir, 'certs')
os.makedirs(certs_dir, exist_ok=True)
cert_file_path = os.path.join(certs_dir, 'cert.pem')
key_file_path = os.path.join(certs_dir, 'key.pem')
current_date_time_for_xml = datetime.now().strftime('%Y%m%d%H%M%S')


def which_time_in_certain_day(patient_code, date_time):
    """
    Получение доступных временных слотов на определенный день.
    Использует предварительную проверку наличия свободных окон через кэшированный DOCT_SCHEDULE_FREE.

    Args:
        patient_code (str/int): Код пациента
        date_time (str): Дата в формате YYYY-MM-DD или "today"/"tomorrow"

    Returns:
        dict/JsonResponse: Информация о доступных слотах или соответствующая ошибка
    """
    from reminder.openai_assistant.api_views import get_date_relation

    global doctor_name
    logger.info(
        f"Я в функции which_time_in_certain_day\nПришедшие данные: \npatient_code: {patient_code}\ndate_time: {date_time}")

    try:
        # Преобразуем date_time в нужный формат, если указаны специальные значения
        if date_time == "today":
            date_str = datetime.now().strftime("%Y-%m-%d")
        elif date_time == "tomorrow":
            date_str = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        else:
            date_str = date_time

        # Проверяем кэш для доступных слотов
        cached_schedule = get_cached_schedule(patient_code)

        if cached_schedule:
            logger.info(f"Используем кэшированные данные для проверки доступности слотов на {date_str}")
            day_check = check_day_has_slots_from_cache(patient_code, date_str, cached_schedule)
        else:
            # Если кэша нет, делаем запрос, который автоматически кэширует результат
            logger.info(f"Кэш не найден, выполняем запрос DOCT_SCHEDULE_FREE для {date_str}")
            day_check = check_day_has_free_slots(patient_code, date_str)

        # Если на этот день нет свободных окон, сразу возвращаем соответствующий статус
        if not day_check.get('has_slots', False):
            # Определяем отношение даты к текущему дню (сегодня/завтра)
            date_obj = datetime.strptime(date_str, '%Y-%m-%d')
            relation = get_date_relation(date_obj)

            # Используем существующие статусы ответов
            if relation == "today":
                return {"status": "error_empty_windows_today", "message": "Свободных приемов на сегодня не найдено.",
                        "day": "сегодня", "day_kz": "бүгін"}
            elif relation == "tomorrow":
                return {"status": "error_empty_windows_tomorrow", "message": "Свободных приемов на завтра не найдено.",
                        "day": "завтра", "day_kz": "ертең"}
            else:
                return {"status": "error_empty_windows", "message": f"Свободных приемов на {date_str} не найдено."}

        # Продолжаем стандартную логику, когда слоты доступны
        if len(date_time) == 10:
            date_time += " 00:00"

        try:
            date_time_obj = datetime.strptime(date_time, '%Y-%m-%d %H:%M')
        except ValueError as e:
            return JsonResponse({'status': 'error', 'message': f'Ошибка преобразования даты: {e}'}, status=400)

        try:
            patient = Patient.objects.get(patient_code=patient_code)
        except Patient.DoesNotExist:
            return JsonResponse({'status': 'error', 'message': f'Пациент с кодом {patient_code} не найден'}, status=404)

        # КРИТИЧЕСКИ ВАЖНО: Приоритет выбора врача
        doctor_code = None
        doctor_name = None

        # 1. Сначала проверяем сохраненную ассоциацию врача
        association = PatientDoctorAssociation.objects.filter(patient=patient).first()
        if association and association.doctor:
            doctor_code = association.doctor.doctor_code
            doctor_name = association.doctor.full_name
            logger.info(f"Найден доктор из ассоциации: {doctor_name} (ID: {doctor_code})")

        # 2. Если нет ассоциации, ищем врача в last_used_doctor
        if not doctor_code and patient.last_used_doctor:
            doctor_code = patient.last_used_doctor.doctor_code
            doctor_name = patient.last_used_doctor.full_name
            logger.info(f"Использование последнего врача из БД: {doctor_name} (ID: {doctor_code})")

        # 3. Если нет врача в БД, пытаемся получить из кэшированных данных
        if not doctor_code and day_check.get('doctor_code'):
            doctor_code = day_check.get('doctor_code')
            logger.info(f"Получен доктор из кэша: {doctor_code}")

            # Пытаемся получить врача из БД или создать нового
            try:
                doctor_obj = Doctor.objects.get(doctor_code=doctor_code)
                logger.info(f"Найден объект врача: {doctor_obj.full_name}")
            except Doctor.DoesNotExist:
                # Получаем имя врача из кэшированных данных если есть
                doctor_name = f'Doctor {doctor_code}'
                if cached_schedule and 'by_doctor' in cached_schedule and str(doctor_code) in cached_schedule[
                    'by_doctor']:
                    doctor_name = cached_schedule['by_doctor'][str(doctor_code)].get('doctor_name', doctor_name)

                doctor_obj, created = Doctor.objects.get_or_create(
                    doctor_code=doctor_code,
                    defaults={'full_name': doctor_name}
                )
                logger.info(f"Создан объект врача: {doctor_obj.full_name} (ID: {doctor_obj.doctor_code})")

            # Обновляем последнего использованного врача для пациента
            patient.last_used_doctor = doctor_obj
            patient.save()

            # Создаем ассоциацию если её нет
            PatientDoctorAssociation.objects.get_or_create(
                patient=patient,
                defaults={'doctor': doctor_obj}
            )

            logger.info(f"Врач {doctor_obj.full_name} закреплен за пациентом {patient_code}")

        # 4. Если все еще нет врача, ищем в активных записях
        if not doctor_code:
            appointment = Appointment.objects.filter(patient=patient, is_active=True).first()
            if appointment and appointment.doctor:
                doctor_code = appointment.doctor.doctor_code
                doctor_name = appointment.doctor.full_name
                logger.info(f"Найден доктор из активной записи: {doctor_name} (ID: {doctor_code})")

        # 5. Если не нашли в записях, проверяем в очереди
        if not doctor_code:
            queue_entry = QueueInfo.objects.filter(patient=patient).first()
            if queue_entry and queue_entry.doctor_code:
                doctor_code = queue_entry.doctor_code
                doctor_name = queue_entry.doctor_name
                logger.info(f"Найден доктор из очереди: {doctor_name} (ID: {doctor_code})")

        # Если доктор не найден, возвращаем ошибку
        if not doctor_code:
            logger.info('Доктор и его код не найден')
            return JsonResponse({'status': 'error', 'message': 'Не найден доктор для пациента'}, status=400)

        # КРИТИЧЕСКИ ВАЖНО: Получаем TOFILIAL (целевой филиал)
        target_branch_id = None

        # Пытаемся получить филиал из кэшированных данных
        if day_check.get('clinic_id'):
            target_branch_id = day_check.get('clinic_id')
            logger.info(f"Получен TOFILIAL из кэша: {target_branch_id}")
        else:
            # Сначала ищем в очередях
            queue_entry = QueueInfo.objects.filter(patient=patient).order_by('-created_at').first()
            if queue_entry and queue_entry.target_branch:
                target_branch_id = queue_entry.target_branch.clinic_id
                logger.info(f"Найден TOFILIAL из очереди: {target_branch_id}")

            # Если не нашли в очередях, смотрим в записях
            appointment = Appointment.objects.filter(patient=patient, is_active=True).first()
            if target_branch_id is None and appointment and appointment.clinic:
                target_branch_id = appointment.clinic.clinic_id
                logger.info(f"Найден TOFILIAL из записи: {target_branch_id}")

            # Если всё равно не нашли, используем значение по умолчанию с предупреждением
            if target_branch_id is None:
                target_branch_id = 1  # Значение по умолчанию
                logger.warning(f"⚠ КРИТИЧЕСКАЯ ОШИБКА: TOFILIAL не найден для пациента {patient_code}!")
                logger.warning("⚠ Используем значение по умолчанию 1, но это может привести к ошибкам!")

        # Получаем имя врача
        if doctor_name is None:
            doctor_name = format_doctor_name(patient_code)

        formatted_doc_name_final = doctor_name if doctor_name else format_doctor_name(patient_code)

        # Определяем текущую дату и завтрашний день
        current_datetime = datetime.now(pytz.timezone("Europe/Moscow"))
        current_date_only = current_datetime.date()
        tomorrow_date_only = current_date_only + timedelta(days=1)
        requested_date_only = date_time_obj.date()

        # Определяем статус на основе даты
        if requested_date_only == current_date_only:
            response_status = 'which_time_today'
            day_for_return = 'сегодня'
        elif requested_date_only == tomorrow_date_only:
            response_status = 'which_time_tomorrow'
            day_for_return = 'завтра'
        else:
            response_status = 'which_time'
            day_for_return = 'null'

        # XML-запрос для получения расписания с использованием TOFILIAL в MSH.99
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
            <MSH.99>{target_branch_id}</MSH.99>
          </MSH>
          <SCHEDULE_IN>
            <INDOCTLIST>{doctor_code}</INDOCTLIST>
            <BDATE>{date_time_obj.strftime('%Y%m%d')}</BDATE>
            <FDATE>{date_time_obj.strftime('%Y%m%d')}</FDATE>
            <EXTINTERV>30</EXTINTERV>
            <SCHLIST/>
          </SCHEDULE_IN>
        </WEB_SCHEDULE>
        '''

        logger.info(f"Полный XML-запрос SCHEDULE: {xml_request}")
        logger.info(f"Используем TOFILIAL={target_branch_id}, doctor_code={doctor_code}")

        # Выполнение POST-запроса
        response = requests.post(
            url=infoclinica_api_url,
            headers={'X-Forwarded-Host': f'{infoclinica_x_forwarded_host}', 'Content-Type': 'text/xml'},
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

            # Сохраняем schedident в кэш для будущего использования
            if schedident_text and cached_schedule and 'by_doctor' in cached_schedule:
                by_doctor = cached_schedule.get('by_doctor', {}).get(str(doctor_code), {})
                schedules = by_doctor.get('schedules', [])

                # Обновляем существующую запись или добавляем новую
                schedule_updated = False
                for schedule in schedules:
                    if schedule.get('date_iso') == date_str:
                        schedule['schedule_id'] = schedident_text
                        schedule_updated = True
                        break

                if not schedule_updated:
                    schedules.append({
                        'date_iso': date_str,
                        'schedule_id': schedident_text,
                        'has_free_slots': True  # Обновится при обработке free_time_intervals
                    })

                by_doctor['schedules'] = schedules
                cached_schedule['by_doctor'][str(doctor_code)] = by_doctor
                cache_schedule(patient_code, cached_schedule)
                logger.info(f"Сохранен schedident {schedident_text} в кэш для врача {doctor_code}")

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

            # Сохраняем доступные времена в БД для этого пациента
            # Сначала удаляем все существующие записи для этой даты
            AvailableTimeSlot.objects.filter(
                patient=patient,
                date=requested_date_only
            ).delete()

            # Получаем объект клиники по target_branch_id
            clinic_obj = None
            if target_branch_id:
                try:
                    clinic_obj = Clinic.objects.get(clinic_id=target_branch_id)
                except Clinic.DoesNotExist:
                    logger.warning(f"Клиника с ID {target_branch_id} не найдена")

            # Получаем объект врача для записи в AvailableTimeSlot
            doctor_obj = None
            if doctor_code:
                try:
                    doctor_obj = Doctor.objects.get(doctor_code=doctor_code)
                except Doctor.DoesNotExist:
                    logger.warning(f"Врач с кодом {doctor_code} не найден")

            for interval in free_time_intervals:
                time_str = interval["start_time"]
                hour, minute = map(int, time_str.split(':'))
                time_obj = dt_time(hour, minute)

                # Используем get_or_create для избежания ошибок UNIQUE constraint
                AvailableTimeSlot.objects.get_or_create(
                    patient=patient,
                    date=requested_date_only,
                    time=time_obj,
                    defaults={
                        'doctor': doctor_obj,
                        'clinic': clinic_obj
                    }
                )

            # Получаем все доступные времена для отображения
            all_available_times = []
            for interval in free_time_intervals:
                time_value = interval["start_time"]
                # Проверка на рабочее время (9:00-21:00)
                hour, minute = map(int, time_value.split(':'))
                if 9 <= hour < 21:  # Только время с 9:00 до 20:59
                    all_available_times.append(time_value)

            # Если интервалы пусты
            if not all_available_times:
                formatted_date = format_russian_date(date_time_obj)
                return JsonResponse({
                    'status': f'error_empty_windows_{response_status.split("_")[-1]}',
                    'message': f'На дату {formatted_date} нет доступных окон.',
                    'time_1': None,
                    'time_2': None,
                    'time_3': None,
                    'day': day_for_return,
                    'specialist_name': formatted_doc_name_final
                })

            # Формирование ответа
            formatted_date = format_russian_date(date_time_obj)
            days = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота', 'Воскресенье']
            weekday = days[date_time_obj.weekday()]

            # Определяем корректный статус ответа в зависимости от количества доступных времен
            if len(all_available_times) == 1:
                if response_status == 'which_time_today':
                    response_status = 'only_first_time_today'
                elif response_status == 'which_time_tomorrow':
                    response_status = 'only_first_time_tomorrow'
                else:
                    response_status = 'only_first_time'
            elif len(all_available_times) == 2:
                if response_status == 'which_time_today':
                    response_status = 'only_two_time_today'
                elif response_status == 'which_time_tomorrow':
                    response_status = 'only_two_time_tomorrow'
                else:
                    response_status = 'only_two_time'

            response = {
                'status': response_status,
                'message': f'На дату {formatted_date} доступны следующие времена: {", ".join(all_available_times[:5])} и другие',
                'first_time': all_available_times[0] if len(all_available_times) > 0 else None,
                'second_time': all_available_times[1] if len(all_available_times) > 1 else None,
                'third_time': all_available_times[2] if len(all_available_times) > 2 else None,
                'all_available_times': all_available_times,
                'date': formatted_date,
                'doctor': formatted_doc_name_final,
                'specialist_name': formatted_doc_name_final,
                'weekday': weekday,
                'day': day_for_return
            }

            return JsonResponse(response)
        else:
            logger.error(f'Ошибка при запросе: {response.status_code}')
            logger.error(f'Ответ сервера: {response.text}')
            return JsonResponse({
                'status': 'error',
                'message': f'Ошибка при запросе: {response.status_code}'
            })

    except Exception as e:
        logger.error(f"Ошибка в which_time_in_certain_day: {e}", exc_info=True)
        return JsonResponse({"status": "error_med_element", "message": str(e)})
