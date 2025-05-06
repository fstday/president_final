import logging
import pytz
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, time as dt_time
from django.http import JsonResponse
from dotenv import load_dotenv
import os

from reminder.infoclinica_requests.schedule.reserve_reception_for_patient import reserve_reception_for_patient
from reminder.models import Patient, Appointment, QueueInfo, Clinic, AvailableTimeSlot, Doctor
from reminder.infoclinica_requests.utils import format_doctor_name, format_russian_date
from django.utils.timezone import make_aware
# Add imports for schedule caching and doctor availability
from reminder.infoclinica_requests.schedule.doct_schedule_free import get_patient_doctor_schedule, \
    select_best_doctor_from_schedules, check_day_has_slots_from_cache
from reminder.infoclinica_requests.schedule.schedule_cache import get_cached_schedule, cache_schedule

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
    Обработка запроса для получения доступных интервалов на определенный день.
    Модифицирована для правильного использования TOFILIAL и обработки ограничений.
    """
    global doctor_name
    logger.info(
        f"Я в функции which_time_in_certain_day\nПришедшие данные: \npatient_code: {patient_code}\ndate_time: {date_time}")

    # Нормализуем формат даты
    if isinstance(date_time, str):
        if len(date_time) == 10:
            date_time += " 00:00"

        if date_time.lower() == 'today':
            date_time = datetime.now().strftime('%Y-%m-%d 00:00')
        elif date_time.lower() == 'tomorrow':
            date_time = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d 00:00')

    try:
        date_time_obj = datetime.strptime(date_time, '%Y-%m-%d %H:%M')
    except ValueError as e:
        logger.error(f'Ошибка преобразования даты: {e}')
        return JsonResponse({'status': 'error', 'message': f'Ошибка преобразования даты: {e}'}, status=400)

    try:
        patient = Patient.objects.get(patient_code=patient_code)
    except Patient.DoesNotExist:
        logger.error(f'Пациент с кодом {patient_code} не найден')
        return JsonResponse({'status': 'error', 'message': f'Пациент с кодом {patient_code} не найден'}, status=404)

    # Переменные для отслеживания состояния
    doctor_code = None
    doctor_name = None
    target_branch_id = None
    requires_referral = False
    has_restrictions = False
    restriction_message = ""
    appointment = None  # Initialize appointment variable

    # ВАЖНОЕ ИЗМЕНЕНИЕ: Проверка и использование кэшированных данных о доступных врачах
    try:
        # Конвертировать дату в строковый формат
        date_str = date_time_obj.strftime('%Y-%m-%d')

        # Переменная для отслеживания требуется ли направление
        requires_referral = False
        referral_message = ""
        appointment = None  # Initialize appointment variable

        # Проверяем кэш для всего периода (7 дней)
        cached_result = get_cached_schedule(patient_code)

        if cached_result:
            logger.info(f"Найден кэш расписания для пациента {patient_code}, проверяем доступность на {date_str}")

            # Проверяем доступность слотов из кэша
            cache_check = check_day_has_slots_from_cache(patient_code, date_str, cached_result)

            if cache_check and cache_check.get('has_slots'):
                doctor_code = cache_check.get('doctor_code')
                department_id = cache_check.get('department_id')
                target_branch_id = cache_check.get('clinic_id')

                # Получаем имя врача
                try:
                    doc = Doctor.objects.get(doctor_code=doctor_code)
                    doctor_name = doc.full_name
                except Doctor.DoesNotExist:
                    doctor_name = f"Врач {doctor_code}"

                logger.info(f"✅ Из кэша выбран врач: {doctor_name} (ID: {doctor_code}), отделение: {department_id}")
            else:
                logger.info(f"⚠ В кэше не найдено слотов на {date_str}, запрашиваем свежие данные")
                cached_result = None

        # Если в кэше нет данных или нет доступных слотов на нужную дату, делаем новый запрос
        if not cached_result or not cached_result.get('success', False):
            # Сначала получаем расписание всех врачей для пациента на 7 дней
            logger.info(f"Вызываем get_patient_doctor_schedule для получения доступных врачей на 7 дней")
            schedule_result = get_patient_doctor_schedule(patient_code, days_horizon=7)

            if schedule_result.get('success', False):
                # Кэшируем результат для последующих запросов
                cache_schedule(patient_code, schedule_result)
                logger.info(f"Результат запроса DOCT_SCHEDULE_FREE закэширован на 15 минут")

                # Проверяем наличие слотов на указанную дату
                cache_check = check_day_has_slots_from_cache(patient_code, date_str, schedule_result)

                if cache_check and cache_check.get('has_slots'):
                    doctor_code = cache_check.get('doctor_code')
                    department_id = cache_check.get('department_id')
                    target_branch_id = cache_check.get('clinic_id')

                    # Получаем имя врача
                    try:
                        doc = Doctor.objects.get(doctor_code=doctor_code)
                        doctor_name = doc.full_name
                    except Doctor.DoesNotExist:
                        doctor_name = f"Врач {doctor_code}"

                    logger.info(
                        f"✅ Из свежих данных выбран врач: {doctor_name} (ID: {doctor_code}), отделение: {department_id}")
                else:
                    logger.error(f"❌ Не найдено врачей с доступными слотами на {date_str}")
                    return JsonResponse({
                        'status': f'error_empty_windows_{date_str.split("-")[-1] if "empty_windows_" in date_str else ""}',
                        'message': f'На дату {date_str} нет доступных слотов для записи'
                    })
            else:
                logger.error(
                    f"❌ Не удалось получить расписание врачей: {schedule_result.get('error', 'Неизвестная ошибка')}")
                # Продолжаем со старой логикой в случае ошибки запроса расписания
                doctor_code = None
                doctor_name = None
                target_branch_id = None
    except Exception as e:
        logger.error(f"❌ Ошибка при получении доступных врачей: {e}", exc_info=True)

        # Продолжаем со старой логикой в случае ошибки
        # Получение информации о враче с поддержкой нескольких врачей
        doctor_code = None
        doctor_name = None
        target_branch_id = None

    # Получение информации о враче с поддержкой нескольких врачей (продолжение существующей логики)
    if not doctor_code:
        # Сначала проверяем в записях
        appointment = Appointment.objects.filter(patient=patient, is_active=True).first()
        if appointment:
            if appointment.doctor:
                doctor_code = appointment.doctor.doctor_code
                doctor_name = appointment.doctor.full_name
                logger.info(f"Найден доктор из активной записи: {doctor_name} (ID: {doctor_code})")

            # Получаем филиал
            if appointment.clinic:
                target_branch_id = appointment.clinic.clinic_id
                logger.info(f"Найден TOFILIAL из записи: {target_branch_id}")

        # Если не нашли в записях, проверяем в очереди
        if doctor_code is None:
            queue_entry = QueueInfo.objects.filter(patient=patient).first()
            if queue_entry:
                if queue_entry.doctor_code:
                    doctor_code = queue_entry.doctor_code
                    doctor_name = queue_entry.doctor_name
                    logger.info(f"Найден доктор из очереди: {doctor_name} (ID: {doctor_code})")

                # Получаем филиал
                if queue_entry.target_branch:
                    target_branch_id = queue_entry.target_branch.clinic_id
                    logger.info(f"Найден TOFILIAL из очереди: {target_branch_id}")

                # Получаем отделение
                if queue_entry.department_number:
                    logger.info(f"Найдено отделение {queue_entry.department_number} в очереди пациента {patient_code}")

        # Если не нашли врача, проверяем последнего использованного врача
        if doctor_code is None and patient.last_used_doctor:
            doctor_code = patient.last_used_doctor.doctor_code
            doctor_name = patient.last_used_doctor.full_name
            logger.info(f"Используем последнего использованного врача: {doctor_name} (ID: {doctor_code})")

    # Если до сих пор нет доктора, логируем ошибку
    if doctor_code is None:
        logger.error('Доктор и его код не найден')
        return JsonResponse({'status': 'error', 'message': 'Не найден доктор для пациента'}, status=400)

    # Если до сих пор нет филиала, используем значение по умолчанию
    if target_branch_id is None:
        target_branch_id = 1  # Значение по умолчанию
        logger.warning(f"⚠ КРИТИЧЕСКАЯ ОШИБКА: TOFILIAL не найден для пациента {patient_code}!")
        logger.warning("⚠ Используем значение по умолчанию 1, но это может привести к ошибкам!")

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

    logger.info(f"Полный XML-запрос which_time_in_certain_day: \n{xml_request}")
    logger.info(
        f"Отправка запроса с TOFILIAL={target_branch_id}, doctor_code={doctor_code}, date={date_time_obj.strftime('%Y%m%d')}")

    # Выполнение POST-запроса
    response = requests.post(
        url=infoclinica_api_url,
        headers={'X-Forwarded-Host': f'{infoclinica_x_forwarded_host}', 'Content-Type': 'text/xml'},
        data=xml_request,
        cert=(cert_file_path, key_file_path)
    )

    # Проверка на ошибки и вывод ответа сервера
    logger.info(f"Получен ответ SCHEDULE (status code): {response.status_code}")

    if response.status_code == 200:
        logger.info(f"Полный ответ SCHEDULE: \n{response.text}")

        root = ET.fromstring(response.text)
        namespace = {'ns': 'http://sdsys.ru/'}

        # Проверяем, требуется ли направление
        requires_referral = False
        referral_message = ""
        check_data = root.findall('.//ns:CHECKDATA', namespace)
        for check in check_data:
            check_code = check.find('ns:CHECKCODE', namespace)
            check_label = check.find('ns:CHECKLABEL', namespace)
            check_text = check.find('ns:CHECKTEXT', namespace)

            if check_code is not None and check_code.text == "2":
                if check_label is not None and "направлени" in check_label.text.lower():
                    requires_referral = True
                    referral_message = check_text.text if check_text is not None else "Требуется направление"
                    logger.warning(f"Для врача {doctor_code} требуется направление: {referral_message}")

        # Если требуется направление, нужно попробовать другого врача
        if requires_referral:
            logger.warning(f"Врач {doctor_code} требует направление, пробуем найти другого доступного врача")

            # Получаем все доступные расписания для пациента
            schedule_result = get_patient_doctor_schedule(patient_code, days_horizon=1)

            if schedule_result.get('success', False) and schedule_result.get('by_doctor'):
                # Перебираем всех врачей и ищем того, кто не требует направления
                tried_alternate_doctor = False

                for alt_doctor_code, doctor_data in schedule_result.get('by_doctor', {}).items():
                    # Пропускаем текущего врача, который требует направление
                    if alt_doctor_code == doctor_code:
                        continue

                    # Проверяем, есть ли у этого врача доступные слоты на нужную дату
                    has_slots_for_date = False
                    for schedule in doctor_data.get('schedules', []):
                        if schedule.get('date_iso') == date_str and schedule.get('has_free_slots', False):
                            has_slots_for_date = True
                            break

                    if has_slots_for_date:
                        # Проверяем, требует ли этот врач направление
                        requires_referral_alt = False
                        for schedule in doctor_data.get('schedules', []):
                            if 'check_data' in schedule:
                                check_data_list = schedule.get('check_data', [])
                                for check in check_data_list:
                                    if check.get('check_code') == '2':
                                        requires_referral_alt = True
                                        break
                            if requires_referral_alt:
                                break

                        # Если этот врач не требует направление, используем его
                        if not requires_referral_alt:
                            doctor_code = alt_doctor_code
                            doctor_name = doctor_data.get('doctor_name', f"Врач {alt_doctor_code}")
                            department_id = doctor_data.get('department_id')
                            logger.info(
                                f"✅ Найден альтернативный врач, не требующий направления: {doctor_name} (ID: {doctor_code})")

                            # Обновляем XML-запрос с новым врачом
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

                            logger.info(f"Повторный запрос с альтернативным врачом: \n{xml_request}")

                            # Выполняем новый запрос с альтернативным врачом
                            response = requests.post(
                                url=infoclinica_api_url,
                                headers={'X-Forwarded-Host': f'{infoclinica_x_forwarded_host}',
                                         'Content-Type': 'text/xml'},
                                data=xml_request,
                                cert=(cert_file_path, key_file_path)
                            )

                            if response.status_code == 200:
                                logger.info(f"Получен ответ для альтернативного врача: {response.status_code}")
                                root = ET.fromstring(response.text)
                                tried_alternate_doctor = True
                                requires_referral = False
                                break

                # Если не удалось найти альтернативного врача, продолжаем с текущим (показываем доступные слоты, но с предупреждением)
                if not tried_alternate_doctor:
                    logger.warning(f"Не найдено альтернативных врачей без требования направления, продолжаем с текущим")
                    # Продолжаем обработку ниже с предупреждением
            else:
                logger.warning(f"Не удалось получить список доступных врачей, продолжаем с текущим")
                # Продолжаем обработку ниже с предупреждением

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

        # Сохраняем доступные времена в БД для этого пациента
        # Сначала удаляем все существующие записи для этой даты
        AvailableTimeSlot.objects.filter(
            patient=patient,
            date=requested_date_only
        ).delete()

        # Затем сохраняем новые
        # Получаем объект врача
        doctor_obj = None
        clinic_obj = None

        # Получаем объект врача если есть doctor_code
        if doctor_code:
            try:
                doctor_obj = Doctor.objects.get(doctor_code=doctor_code)
            except Doctor.DoesNotExist:
                # Если врач не существует в БД, создадим его запись
                doctor_obj = Doctor.objects.create(
                    doctor_code=doctor_code,
                    full_name=doctor_name if doctor_name else f"Врач {doctor_code}"
                )
                logger.info(f"Создан новый врач в БД: {doctor_obj.full_name} (ID: {doctor_code})")

        # Получаем объект клиники по target_branch_id
        if target_branch_id:
            try:
                clinic_obj = Clinic.objects.get(clinic_id=target_branch_id)
            except Clinic.DoesNotExist:
                # Если клиника не существует, создадим её запись
                clinic_obj = Clinic.objects.create(
                    clinic_id=target_branch_id,
                    name=f"Клиника {target_branch_id}"
                )
                logger.info(f"Создана новая клиника в БД: ID {target_branch_id}")

        for interval in free_time_intervals:
            time_str = interval["start_time"]
            hour, minute = map(int, time_str.split(':'))
            time_obj = dt_time(hour, minute)

            AvailableTimeSlot.objects.create(
                patient=patient,
                date=requested_date_only,
                time=time_obj,
                doctor=doctor_obj,
                clinic=clinic_obj
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
                'specialist_name': formatted_doc_name_final,
                'requires_referral': requires_referral,
                'referral_message': referral_message if requires_referral else None
            })

        # Если после всех попыток найти другого врача у нас все еще есть врач с ограничениями,
        # и это вызов для резервирования (не для просмотра), вернем сообщение об ошибке
        if has_restrictions and not response_status.startswith('which_time'):
            return JsonResponse({
                'status': 'error_restrictions',
                'message': restriction_message,
                'doctor_code': doctor_code,
                'doctor_name': formatted_doc_name_final
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
            'doctor_code': doctor_code,  # Добавляем код врача для последующей записи
            'specialist_name': formatted_doc_name_final,
            'weekday': weekday,
            'day': day_for_return,
            'target_branch_id': target_branch_id,  # Добавляем ID филиала
            'schedident': schedident_text,  # Добавляем schedident для использования в reserve_reception_for_patient
            'requires_referral': requires_referral,  # Добавляем флаг, требуется ли направление
            'referral_message': referral_message if requires_referral else None  # Добавляем сообщение о направлении
        }

        return JsonResponse(response)
    else:
        logger.error(f'Ошибка при запросе: {response.status_code}')
        logger.error(f'Ответ сервера: {response.text}')
        return JsonResponse({
            'status': 'error',
            'message': f'Ошибка при запросе: {response.status_code}'
        })


if __name__ == '__main__':
    which_time_in_certain_day(patient_code=990000612, date_time="2025-03-18")
