import json

from dotenv import load_dotenv
import requests
import os
import django
import logging
import xml.etree.ElementTree as ET
import redis
from datetime import datetime, time
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
    Отправляет XML запрос и резервирует свободное время за клиентом.
    Исправленная версия для работы с несколькими врачами.
    """
    try:
        # Получаем информацию о пациенте
        found_patient = Patient.objects.get(patient_code=patient_id)

        # КРИТИЧЕСКИ ВАЖНО: Получаем target_branch (TOFILIAL) из очереди пациента
        target_branch_id = None

        # Сначала ищем в активных очередях
        latest_queue = QueueInfo.objects.filter(
            patient=found_patient
        ).order_by('-created_at').first()

        if latest_queue and latest_queue.target_branch:
            target_branch_id = latest_queue.target_branch.clinic_id
            logger.info(f"✅ Получен TOFILIAL из очереди: {target_branch_id}")
        else:
            # Если в очереди не нашли, проверяем в записях
            latest_appointment = Appointment.objects.filter(
                patient=found_patient,
                is_active=True
            ).order_by('-created_at').first()

            if latest_appointment and latest_appointment.clinic:
                target_branch_id = latest_appointment.clinic.clinic_id
                logger.info(f"✅ Получен TOFILIAL из записи: {target_branch_id}")

        # Если не нашли ID клиники, используем значение по умолчанию
        if not target_branch_id:
            logger.warning(f"⚠ КРИТИЧЕСКАЯ ОШИБКА: TOFILIAL не найден для пациента {patient_id}!")
            logger.warning("⚠ Используем значение по умолчанию 1, но это может привести к ошибкам!")
            target_branch_id = 1

        # Найти доктора по doctor_id
        doctor = None
        try:
            doctor = Doctor.objects.get(doctor_code=doctor_id)
            logger.info(f"Найден врач: {doctor.full_name}")
        except Doctor.DoesNotExist:
            logger.warning(f"Врач с кодом {doctor_id} не найден")
            # Пробуем создать объект врача с минимальной информацией
            doctor = Doctor.objects.create(
                doctor_code=doctor_id,
                full_name=f"Врач {doctor_id}"
            )
            logger.info(f"Создан новый врач с кодом {doctor_id}")

        # Найти клинику по target_branch_id
        clinic = None
        try:
            clinic = Clinic.objects.get(clinic_id=target_branch_id)
            logger.info(f"Найдена клиника: {clinic.name}")
        except Clinic.DoesNotExist:
            logger.warning(f"Клиника с ID {target_branch_id} не найдена")
            # Пробуем создать объект клиники с минимальной информацией
            clinic = Clinic.objects.create(
                clinic_id=target_branch_id,
                name=f"Клиника {target_branch_id}"
            )
            logger.info(f"Создана новая клиника с ID {target_branch_id}")

        # Получить причину и отделение из QueueInfo, если возможно
        reason = None
        department = None

        if latest_queue:
            # Получаем причину из очереди
            reason = latest_queue.reason
            if reason:
                logger.info(f"Получена причина из очереди: {reason.reason_name}")
            else:
                # Создаем причину "по умолчанию" если нет
                from reminder.models import Reason
                reason, _ = Reason.objects.get_or_create(
                    reason_code=1,
                    defaults={'reason_name': 'Неизвестная причина'}
                )
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

    # КРИТИЧЕСКИ ВАЖНО: Определение текущего дня и завтра для правильного выбора статуса
    today = datetime.now().date()
    tomorrow = today + timedelta(days=1)
    appointment_date = None

    # КРИТИЧЕСКИ ВАЖНО: Правильная обработка даты/времени
    # Если date_obj - это строка, нормализуем её в datetime
    if isinstance(date_obj, str):
        try:
            # Пробуем различные форматы
            if ' ' in date_obj:
                try:
                    date_obj = datetime.strptime(date_obj, '%Y-%m-%d %H:%M')
                    appointment_date = date_obj.date()
                except ValueError:
                    try:
                        date_obj = datetime.strptime(date_obj, '%Y-%m-%d %H:%M:%S')
                        appointment_date = date_obj.date()
                    except ValueError:
                        return {'status': 'error', 'message': f'Неверный формат даты: {date_obj}'}
            else:
                # Это только дата без времени
                date_obj = datetime.strptime(date_obj, '%Y-%m-%d')
                appointment_date = date_obj.date()
        except ValueError as e:
            return {'status': 'error', 'message': f'Ошибка преобразования даты: {str(e)}'}
    elif isinstance(date_obj, datetime):
        appointment_date = date_obj.date()

    # Если result_time - это строка, нормализуем её
    if isinstance(result_time, str):
        try:
            # Если это полная дата/время
            if ' ' in result_time:
                try:
                    result_time = datetime.strptime(result_time, '%Y-%m-%d %H:%M')
                except ValueError:
                    try:
                        result_time = datetime.strptime(result_time, '%Y-%m-%d %H:%M:%S')
                    except ValueError:
                        return {'status': 'error', 'message': f'Неверный формат времени: {result_time}'}
            else:
                # Это только время без даты, используем дату из date_part
                if ':' in result_time:
                    hour, minute = map(int, result_time.split(':')[:2])

                    # Получаем дату из date_part
                    if isinstance(date_part, str) and len(date_part) >= 10:
                        date_only = datetime.strptime(date_part[:10], '%Y-%m-%d').date()
                    elif isinstance(date_obj, datetime):
                        date_only = date_obj.date()
                    else:
                        return {'status': 'error', 'message': 'Не удалось определить дату'}

                    # Создаем полный datetime
                    result_time = datetime.combine(date_only, time(hour, minute))
                else:
                    return {'status': 'error', 'message': f'Неверный формат времени: {result_time}'}
        except ValueError as e:
            return {'status': 'error', 'message': f'Ошибка преобразования времени: {str(e)}'}
    elif not isinstance(result_time, datetime):
        return {'status': 'error', 'message': 'result_time должен быть строкой или объектом datetime'}

    # Гарантируем, что date_obj и result_time совпадают по времени
    # Используем время из result_time, оно более точное
    bhour, bmin = result_time.hour, result_time.minute
    logger.info(f"Время для резервирования: {bhour}:{bmin}")

    # Если date_part - это объект datetime, преобразуем его в строку
    if isinstance(date_part, datetime):
        date_part = date_part.strftime('%Y-%m-%d')

    # Проверяем, что date_part имеет правильный формат
    if not isinstance(date_part, str) or len(date_part) < 10:
        if isinstance(result_time, datetime):
            date_part = result_time.strftime('%Y-%m-%d')
        else:
            return {'status': 'error', 'message': 'Неверный формат date_part'}

    # Преобразуем в формат без '-' для XML запроса
    workdate = date_part.replace('-', '')

    # Устанавливаем FHOUR и FMIN на 30 минут позже
    fmin = bmin + 30
    fhour = bhour

    if fmin >= 60:
        fmin -= 60
        fhour += 1

    # Убедимся, что часы находятся в диапазоне [0, 23]
    if fhour >= 24:
        fhour -= 24

    # Гарантируем, что часы и минуты имеют двузначный формат
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

    # КРИТИЧЕСКОЕ ИЗМЕНЕНИЕ: Используем target_branch_id (TOFILIAL) в MSH.99 запроса
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
        <MSH.99>{target_branch_id}</MSH.99>
      </MSH>
      <SCHEDULE_REC_RESERVE_IN>
        {xml_schedule_reserve_in}
      </SCHEDULE_REC_RESERVE_IN>
    </WEB_SCHEDULE_REC_RESERVE>
    """

    logger.info(f"Полный XML-запрос schedule_rec_reserve: {xml_request}")
    logger.info(f"Параметры: DCODE={doctor_id}, WORKDATE={workdate}, BHOUR={bhour_str}, BMIN={bmin_str}")
    logger.info(
        f"Отправляем запрос на резервирование: DCODE={doctor_id}, WORKDATE={workdate}, BHOUR={bhour_str}, BMIN={bmin_str}, TOFILIAL={target_branch_id}")

    try:
        response = requests.post(
            url=infoclinica_api_url,
            headers=headers,
            data=xml_request,
            cert=(cert_file_path, key_file_path)
        )

        if response.status_code == 200:
            try:
                logger.info(f"Полный ответ schedule_rec_reserve: \n{response.text}")
                root = ET.fromstring(response.text)
                namespace = {'ns': 'http://sdsys.ru/'}

                # Ищем код результата и комментарий
                sp_result_code = root.find('.//ns:SPRESULT', namespace)
                sp_comment_text = root.find('.//ns:SPCOMMENT', namespace)

                # Проверяем, что код результата существует
                if sp_result_code is None:
                    logger.error("SPRESULT не найден в ответе сервера")
                    return {
                        'status': 'error_med_element',
                        'message': 'SPRESULT не найден в ответе сервера'
                    }

                sp_result = int(sp_result_code.text)

                # Получаем комментарий, если есть
                sp_comment = sp_comment_text.text if sp_comment_text is not None else "Нет комментария"

                logger.info(f"Код результата SPRESULT: {sp_result}, комментарий: {sp_comment}")

                # Функции для форматирования даты
                def format_date(date_obj):
                    months_ru = {
                        1: "Января", 2: "Февраля", 3: "Марта", 4: "Апреля", 5: "Мая", 6: "Июня",
                        7: "Июля", 8: "Августа", 9: "Сентября", 10: "Октября", 11: "Ноября", 12: "Декабря"
                    }
                    return f"{date_obj.day} {months_ru[date_obj.month]}"

                def format_date_kz(date_obj):
                    months_kz = {
                        1: "Қаңтар", 2: "Ақпан", 3: "Наурыз", 4: "Сәуір", 5: "Мамыр", 6: "Маусым",
                        7: "Шілде", 8: "Тамыз", 9: "Қыркүйек", 10: "Қазан", 11: "Қараша", 12: "Желтоқсан"
                    }
                    return f"{date_obj.day} {months_kz[date_obj.month]}"

                def get_weekday(date_obj):
                    weekdays_ru = {
                        0: "Понедельник", 1: "Вторник", 2: "Среда", 3: "Четверг",
                        4: "Пятница", 5: "Суббота", 6: "Воскресенье"
                    }
                    return weekdays_ru[date_obj.weekday()]

                def get_weekday_kz(date_obj):
                    weekdays_kz = {
                        0: "Дүйсенбі", 1: "Сейсенбі", 2: "Сәрсенбі", 3: "Бейсенбі",
                        4: "Жұма", 5: "Сенбі", 6: "Жексенбі"
                    }
                    return weekdays_kz[date_obj.weekday()]

                if sp_result == 1:
                    # Успешное резервирование
                    schedid_element = root.find(".//ns:SCHEDID", namespace)
                    schedid_value = None

                    if schedid_element is not None and schedid_element.text:
                        try:
                            schedid_value = int(schedid_element.text)
                            logger.info(f"Получен ID записи (SCHEDID): {schedid_value}")
                        except ValueError:
                            logger.warning(f"Не удалось преобразовать SCHEDID в число: {schedid_element.text}")

                    # Если это новая запись или мы получили SCHEDID
                    if not is_reschedule or schedid_value:
                        # Формируем объект записи
                        appointment_data = {
                            'patient': found_patient,
                            'doctor': doctor,
                            'clinic': clinic,
                            'department': department,
                            'reason': reason,
                            'is_infoclinica_id': True,
                            'start_time': date_obj,
                            'end_time': date_obj + timedelta(minutes=30),
                            'is_active': True
                        }

                        if schedid_value:
                            # Если получили ID записи, используем его
                            appointment, created = Appointment.objects.update_or_create(
                                appointment_id=schedid_value,
                                defaults=appointment_data
                            )
                            logger.info(f"{'Создана' if created else 'Обновлена'} запись на прием: {appointment}")
                        else:
                            # Если не получили ID, создаем запись без привязки к ID
                            appointment = Appointment.objects.create(**appointment_data)
                            logger.info(f"Создана запись без ID: {appointment}")

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
                            old_appointment.end_time = date_obj + timedelta(minutes=30)
                            old_appointment.save()

                            logger.info(f"Обновлена существующая запись: {old_appointment}")
                        except Appointment.DoesNotExist:
                            logger.warning(f"Запись с ID {schedid} не найдена в Appointment")

                    # Заносим время и дату в Redis
                    appointment_time_str = date_obj.strftime('%Y-%m-%d %H:%M:%S')
                    redis_reception_appointment(patient_id=patient_id, appointment_time=appointment_time_str)

                    # ИЗМЕНЕНИЕ: Используем правильный статус в зависимости от даты
                    status_code = "success_change_reception"
                    if appointment_date == today:
                        status_code = "success_change_reception_today"
                    elif appointment_date == tomorrow:
                        status_code = "success_change_reception_tomorrow"

                    # Добавляем информацию о дне для сегодня/завтра
                    day_info = {}
                    if appointment_date == today:
                        day_info = {
                            "day": "сегодня",
                            "day_kz": "бүгін"
                        }
                    elif appointment_date == tomorrow:
                        day_info = {
                            "day": "завтра",
                            "day_kz": "ертең"
                        }

                    # Формируем ответ с правильным статусом и полной информацией
                    return {
                        'status': status_code,
                        'message': f'Запись произведена успешно на {appointment_time_str}',
                        'time': result_time.strftime('%H:%M') if isinstance(result_time, datetime) else result_time,
                        'specialist_name': doctor.full_name if doctor else "Специалист",
                        'date': format_date(appointment_date),
                        'date_kz': format_date_kz(appointment_date),
                        'weekday': get_weekday(appointment_date),
                        'weekday_kz': get_weekday_kz(appointment_date),
                        **day_info
                    }

                elif sp_result == 0:
                    # Время занято - это происходит из-за конфликта с другими запросами
                    logger.info('К сожалению, выбранное время было недавно занято.')

                    # Проверим свободные времена еще раз через вызов which_time_in_certain_day
                    try:
                        from reminder.infoclinica_requests.schedule.which_time_in_certain_day import \
                            which_time_in_certain_day

                        # Получаем текущую дату в формате YYYY-MM-DD
                        current_date = date_obj.strftime('%Y-%m-%d') if isinstance(date_obj, datetime) else date_obj[
                                                                                                            :10]

                        # Запрашиваем свежие данные о свободных интервалах
                        logger.info(
                            f"Запрашиваем актуальные данные о свободных временах для {doctor_id} на {current_date}")
                        fresh_times_result = which_time_in_certain_day(patient_id, current_date)
                        if hasattr(fresh_times_result, 'content'):
                            fresh_times_data = json.loads(fresh_times_result.content.decode('utf-8'))
                        else:
                            fresh_times_data = fresh_times_result

                        # Проверяем наличие свободных времен
                        if 'all_available_times' in fresh_times_data and fresh_times_data['all_available_times']:
                            fresh_free_intervals = []
                            for t in fresh_times_data['all_available_times']:
                                start_hour, start_min = map(int, t.split(':'))
                                # Предполагаем 30-минутные интервалы
                                end_hour = start_hour
                                end_min = start_min + 30
                                if end_min >= 60:
                                    end_min -= 60
                                    end_hour += 1

                                fresh_free_intervals.append({
                                    'start_time': t,
                                    'end_time': f"{end_hour:02d}:{end_min:02d}"
                                })

                            # Используем актуальные свободные интервалы вместо устаревших
                            free_intervals = fresh_free_intervals
                            logger.info(f"Получено {len(free_intervals)} актуальных свободных интервалов")
                    except Exception as e:
                        logger.error(f"Ошибка при обновлении свободных интервалов: {e}")

                    # Определяем ближайшие доступные времена
                    if isinstance(result_time, datetime):
                        requested_time_obj = result_time.time()
                    else:
                        requested_time_obj = datetime.strptime(str(result_time)[:5], "%H:%M").time()

                    # Возвращаем все доступные времена
                    available_times = compare_and_suggest_times(free_intervals, requested_time_obj, date_part)

                    # Определяем статус в зависимости от количества доступных времен и даты
                    if len(available_times) == 0:
                        status = "error_change_reception_bad_date"
                        return {
                            'status': status,
                            'data': f'Время приема {result_time} занято. Нет доступных альтернатив.'
                        }
                    elif len(available_times) == 1:
                        status = "change_only_first_time"
                        if appointment_date == today:
                            status = "change_only_first_time_today"
                        elif appointment_date == tomorrow:
                            status = "change_only_first_time_tomorrow"

                        # Добавляем информацию о дне для сегодня/завтра
                        day_info = {}
                        if appointment_date == today:
                            day_info = {
                                "day": "сегодня",
                                "day_kz": "бүгін"
                            }
                        elif appointment_date == tomorrow:
                            day_info = {
                                "day": "завтра",
                                "day_kz": "ертең"
                            }

                        return {
                            'status': status,
                            'date': format_date(appointment_date),
                            'date_kz': format_date_kz(appointment_date),
                            'weekday': get_weekday(appointment_date),
                            'weekday_kz': get_weekday_kz(appointment_date),
                            'specialist_name': doctor.full_name if doctor else "Специалист",
                            'first_time': available_times[0],
                            'message': f'Время приема {result_time} занято. Предлагаю альтернативное время.',
                            **day_info
                        }
                    elif len(available_times) == 2:
                        status = "change_only_two_time"
                        if appointment_date == today:
                            status = "change_only_two_time_today"
                        elif appointment_date == tomorrow:
                            status = "change_only_two_time_tomorrow"

                        # Добавляем информацию о дне для сегодня/завтра
                        day_info = {}
                        if appointment_date == today:
                            day_info = {
                                "day": "сегодня",
                                "day_kz": "бүгін"
                            }
                        elif appointment_date == tomorrow:
                            day_info = {
                                "day": "завтра",
                                "day_kz": "ертең"
                            }

                        return {
                            'status': status,
                            'date': format_date(appointment_date),
                            'date_kz': format_date_kz(appointment_date),
                            'weekday': get_weekday(appointment_date),
                            'weekday_kz': get_weekday_kz(appointment_date),
                            'specialist_name': doctor.full_name if doctor else "Специалист",
                            'first_time': available_times[0],
                            'second_time': available_times[1],
                            'message': f'Время приема {result_time} занято. Предлагаю альтернативные времена.',
                            **day_info
                        }
                    else:
                        status = "error_change_reception"
                        if appointment_date == today:
                            status = "error_change_reception_today"
                        elif appointment_date == tomorrow:
                            status = "error_change_reception_tomorrow"

                        # Добавляем информацию о дне для сегодня/завтра
                        day_info = {}
                        if appointment_date == today:
                            day_info = {
                                "day": "сегодня",
                                "day_kz": "бүгін"
                            }
                        elif appointment_date == tomorrow:
                            day_info = {
                                "day": "завтра",
                                "day_kz": "ертең"
                            }

                        return {
                            'status': status,
                            'date': format_date(appointment_date),
                            'date_kz': format_date_kz(appointment_date),
                            'weekday': get_weekday(appointment_date),
                            'weekday_kz': get_weekday_kz(appointment_date),
                            'specialist_name': doctor.full_name if doctor else "Специалист",
                            'first_time': available_times[0],
                            'second_time': available_times[1],
                            'third_time': available_times[2],
                            'message': f'Время приема {result_time} занято. Возвращаю альтернативные времена.',
                            **day_info
                        }
                else:
                    # Другие коды ошибок
                    logger.warning(f'Получен код ошибки: {sp_result}, комментарий: {sp_comment}')
                    return {
                        'status': 'error_med_element',
                        'message': f'Ошибка при резервировании: {sp_comment}'
                    }

            except ET.ParseError as e:
                logger.error(f'Ошибка при парсинге XML: {e}')
                return {
                    'status': 'error_med_element',
                    'message': f'Ошибка при парсинге XML: {e}'
                }
        else:
            # Ошибка HTTP
            logger.error(f'HTTP ошибка: {response.status_code}, ответ: {response.text}')
            return {
                'status': 'error_med_element',
                'message': f'HTTP ошибка: {response.status_code}'
            }

    except requests.exceptions.RequestException as e:
        logger.error(f'Ошибка при выполнении запроса: {e}')
        return {
            'status': 'error_med_element',
            'message': f'Ошибка при выполнении запроса: {e}'
        }

    # Если код доходит до сюда, значит, произошла ошибка
    return {
        'status': 'error_med_element',
        'message': 'Не удалось завершить операцию резервирования.'
    }
