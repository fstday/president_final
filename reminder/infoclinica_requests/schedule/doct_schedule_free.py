import os
import django
import logging
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

# Настройки Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'president_final.settings')
django.setup()

# Настройка логирования
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Импорты моделей
from reminder.models import Clinic, Doctor, Department, Patient, QueueInfo
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

    Параметры:
    patient_code (int): Код пациента
    days_horizon (int): Горизонт планирования в днях (7 или 14)
    online_mode (int): Режим приема: 0 - в клинике, 1 - и клиника и онлайн
    return_raw (bool): Если True, возвращает сырой XML-ответ

    Возвращает:
    В зависимости от параметра return_raw:
      - True: строку с XML-ответом
      - False: словарь с результатами запроса или None при ошибке
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

        # Properly format filter lists based on retrieved data
        filial_list = f"<FILIALLIST>{clinic_id}</FILIALLIST>" if clinic_id else "<FILIALLIST></FILIALLIST>"
        dep_list = f"<DEPLIST>{department_id}</DEPLIST>" if department_id else "<DEPLIST></DEPLIST>"
        doct_list = f"<DOCTLIST>{doctor_code}</DOCTLIST>" if doctor_code else "<DOCTLIST></DOCTLIST>"

        # Формируем XML запрос согласно XSD-схеме
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
            {filial_list}
            {dep_list}
            {doct_list}
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

            # Если запрошен сырой ответ, возвращаем его
            if return_raw:
                return response.text

            # Иначе разбираем XML-ответ
            result = parse_doctor_schedule_response(response.text)

            # Если запрашивается расписание на 2 недели, делаем второй запрос
            if days_horizon > 7 and result.get('success', False):
                # Запрашиваем вторую неделю
                second_week_start = current_date + timedelta(days=7)
                second_week_end = current_date + timedelta(days=14)

                bdate_second = second_week_start.strftime("%Y%m%d")
                fdate_second = second_week_end.strftime("%Y%m%d")

                # Новая метка времени и ID сообщения для второго запроса
                ts_2 = datetime.now().strftime("%Y%m%d%H%M%S")
                msh_10_second = generate_msh_10()

                # Формируем запрос на вторую неделю (соблюдая порядок элементов по XSD)
                # Use the same format for the second week request
                xml_request_second = f'''
                <WEB_DOCT_SCHEDULE_FREE xmlns="http://sdsys.ru/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
                  <MSH>
                    <MSH.7>
                      <TS.1>{ts_2}</TS.1>
                    </MSH.7>
                    <MSH.9>
                      <MSG.1>WEB</MSG.1>
                      <MSG.2>DOCT_SCHEDULE_FREE</MSG.2>
                    </MSH.9>
                    <MSH.10>{msh_10_second}</MSH.10>
                    <MSH.18>UTF-8</MSH.18>
                    <MSH.99>{clinic_id if clinic_id else ""}</MSH.99>
                  </MSH>
                  <DOCT_SCHEDULE_FREE_IN>
                    {filial_list}
                    {dep_list}
                    {doct_list}
                    <SCHEDIDENTLIST></SCHEDIDENTLIST>
                    <BDATE>{bdate_second}</BDATE>
                    <FDATE>{fdate_second}</FDATE>
                    <ONLINEMODE>{online_mode}</ONLINEMODE>
                  </DOCT_SCHEDULE_FREE_IN>
                </WEB_DOCT_SCHEDULE_FREE>
                '''

                logger.info(f"Отправка второго запроса DOCT_SCHEDULE_FREE (вторая неделя):\n{xml_request_second}")

                # Выполняем второй запрос
                response_second = requests.post(
                    url=infoclinica_api_url,
                    headers={
                        'X-Forwarded-Host': infoclinica_x_forwarded_host,
                        'Content-Type': 'text/xml'
                    },
                    data=xml_request_second,
                    cert=(cert_file_path, key_file_path),
                    verify=True
                )

                # Обрабатываем результат второго запроса
                if response_second.status_code == 200:
                    logger.info(f"Получен ответ для второй недели (длина: {len(response_second.text)} символов)")

                    # Если запрошен сырой ответ, возвращаем оба ответа
                    if return_raw:
                        return {"week1": response.text, "week2": response_second.text}

                    # Объединяем результаты обоих запросов
                    second_week_result = parse_doctor_schedule_response(response_second.text)
                    if second_week_result and second_week_result.get('success',
                                                                     False) and 'schedules' in second_week_result:
                        result['schedules'].extend(second_week_result['schedules'])
                else:
                    logger.error(f"❌ Ошибка при запросе второй недели: {response_second.status_code}")
                    logger.error(f"Ответ сервера: {response_second.text}")

            return result
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
    Определяет доступного врача для пациента

    Параметры:
    patient_code (int): Код пациента

    Возвращает:
    tuple: (doctor_code, department_id, clinic_id)
    """
    # Пытаемся найти связанную информацию о пациенте
    try:
        # Проверяем очереди пациента
        queues = QueueInfo.objects.filter(patient__patient_code=patient_code).order_by('-created_at')

        if queues.exists():
            latest_queue = queues.first()

            # Приоритеты:
            # 1. Врач + отделение + филиал из очереди
            if latest_queue.doctor_code and latest_queue.department_number and latest_queue.target_branch:
                return (
                    latest_queue.doctor_code,
                    latest_queue.department_number,
                    latest_queue.target_branch.clinic_id
                )

            # 2. Врач + филиал
            if latest_queue.doctor_code and latest_queue.target_branch:
                return (
                    latest_queue.doctor_code,
                    None,
                    latest_queue.target_branch.clinic_id
                )

            # 3. Отделение + филиал
            if latest_queue.department_number and latest_queue.target_branch:
                return (
                    None,
                    latest_queue.department_number,
                    latest_queue.target_branch.clinic_id
                )

            # 4. Только филиал
            if latest_queue.target_branch:
                return (None, None, latest_queue.target_branch.clinic_id)

        # Если не нашли в очередях, проверяем пациента напрямую
        patient = Patient.objects.filter(patient_code=patient_code).first()
        if patient:
            # Проверяем активные записи пациента
            appointments = patient.appointments.filter(is_active=True).order_by('-start_time')
            if appointments.exists():
                latest_appointment = appointments.first()

                # Приоритеты как в случае с очередями
                if latest_appointment.doctor and latest_appointment.department and latest_appointment.clinic:
                    return (
                        latest_appointment.doctor.doctor_code,
                        latest_appointment.department.department_id,
                        latest_appointment.clinic.clinic_id
                    )

                if latest_appointment.doctor and latest_appointment.clinic:
                    return (
                        latest_appointment.doctor.doctor_code,
                        None,
                        latest_appointment.clinic.clinic_id
                    )

                if latest_appointment.department and latest_appointment.clinic:
                    return (
                        None,
                        latest_appointment.department.department_id,
                        latest_appointment.clinic.clinic_id
                    )

                if latest_appointment.clinic:
                    return (None, None, latest_appointment.clinic.clinic_id)

    except Exception as e:
        logger.error(f"❌ Ошибка при определении врача для пациента: {e}", exc_info=True)

    # Если ничего не нашли, возвращаем None для всех значений
    return (None, None, None)


def parse_doctor_schedule_response(xml_response):
    """
    Разбор XML-ответа для запроса DOCT_SCHEDULE_FREE

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
                'schedules': []
            }

        schedules = []

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

            # Форматируем дополнительные поля для удобства
            if 'date' in schedule_data:
                try:
                    date_str = schedule_data['date']
                    if len(date_str) == 8:  # Формат YYYYMMDD
                        year, month, day = int(date_str[0:4]), int(date_str[4:6]), int(date_str[6:8])

                        # Добавляем форматированные значения для удобства
                        schedule_data['date_formatted'] = f"{day:02d}.{month:02d}.{year}"

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

        # Сортируем результаты по дате и времени
        schedules.sort(key=lambda x: (x.get('date', ''), x.get('begin_hour', 0), x.get('begin_min', 0)))

        return {
            'success': True,
            'schedules': schedules
        }

    except Exception as e:
        logger.error(f"❌ Ошибка при разборе ответа: {e}", exc_info=True)
        return {
            'success': False,
            'error': f"Ошибка при разборе ответа: {str(e)}"
        }


def print_doctor_schedule(schedule_data):
    """
    Вспомогательная функция для вывода графика в удобном формате.

    Параметры:
    schedule_data (dict): Данные графика работы
    """
    if not schedule_data or not schedule_data.get('success', False):
        error = schedule_data.get('error', 'Неизвестная ошибка') if schedule_data else 'Нет данных'
        print(f"❌ Не удалось получить график: {error}")
        return

    schedules = schedule_data.get('schedules', [])
    if not schedules:
        print("ℹ️ График работы пуст или недоступен")
        return

    print(f"\n{'=' * 80}")
    print(f"ГРАФИК РАБОТЫ (всего интервалов: {len(schedules)})")
    print(f"{'=' * 80}")

    # Группируем по дате, врачу и клинике
    current_date = None
    current_doctor = None
    current_clinic = None

    for slot in schedules:
        date = slot.get('date_formatted')
        doctor = f"{slot.get('doctor_name')} (ID: {slot.get('doctor_code')})"
        clinic = f"{slot.get('clinic_name')} (ID: {slot.get('clinic_id')})"

        if date != current_date:
            print(f"\n{'-' * 80}")
            print(f"📅 ДАТА: {date}")
            print(f"{'-' * 80}")
            current_date = date
            current_doctor = None
            current_clinic = None

        if doctor != current_doctor or clinic != current_clinic:
            print(f"\n👨‍⚕️ ВРАЧ: {doctor}")
            print(f"🏥 Филиал: {clinic}")
            print(f"🏢 Отделение: {slot.get('department_name')} (ID: {slot.get('department_id')})")
            current_doctor = doctor
            current_clinic = clinic

        # Цветовая индикация свободных/занятых интервалов
        slot_indicator = "✅" if slot.get('has_free_slots', False) else "❌"
        room_info = f"Кабинет: {slot.get('room_num', '-')}"
        if 'room_floor' in slot and slot['room_floor']:
            room_info += f", Этаж: {slot['room_floor']}"
        if 'room_building' in slot and slot['room_building']:
            room_info += f", Корпус: {slot['room_building']}"

        free_info = ""
        if 'free_count' in slot and slot['free_count'] > 0:
            free_info = f"Свободно слотов: {slot['free_count']}"

        print(f"{slot_indicator} {slot.get('begin_time')} - {slot.get('end_time')} "
              f"({slot.get('duration_minutes', '-')} мин.) "
              f"{room_info} | {slot.get('online_mode_text', '-')} | {free_info}")

    print(f"\n{'=' * 80}\n")


# Пример использования
if __name__ == "__main__":
    # Пример с пациентом из реальных данных
    patient_code = 990000735  # Пример: Тест Иван Иванович

    # Вариант 1: Получение обработанного расписания
    print("\nПОЛУЧЕНИЕ ГРАФИКА РАБОТЫ ДЛЯ ПАЦИЕНТА (обработанный вывод)")
    patient_schedule = get_patient_doctor_schedule(patient_code)
    print_doctor_schedule(patient_schedule)

    # Вариант 2: Получение сырого XML-ответа
    print("\nПОЛУЧЕНИЕ ГРАФИКА РАБОТЫ ДЛЯ ПАЦИЕНТА (сырой XML)")
    raw_response = get_patient_doctor_schedule(patient_code, return_raw=True)
    print(raw_response)
