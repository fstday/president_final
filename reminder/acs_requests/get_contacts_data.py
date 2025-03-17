from datetime import timedelta, datetime
import pytz
from reminder.models import Appointment
from reminder.properties.utils import get_formatted_date_info


def get_order_data_for_appointment(appointment):
    """
    Получает данные для создания заказа в ACS по данным из новой модели Appointment.
    """
    print('Я в get_order_data_for_appointment')
    order_list = []

    # Формируем полное имя пациента
    full_name = appointment.patient.get_full_name()

    # Преобразование времени приема в UTC+5
    tz_utc_plus_5 = pytz.timezone('Asia/Yekaterinburg')
    reception_start_time = appointment.start_time.astimezone(tz_utc_plus_5)
    date_object = reception_start_time + timedelta(hours=5)
    reception_time_for_api = reception_start_time.strftime('%Y-%m-%d %H:%M')

    # Форматирование в нужный формат
    start_time = reception_start_time.strftime("%Y-%m-%d %H:%M")

    reception_date = reception_start_time.date()
    reception_time = reception_start_time.strftime("%H:%M")

    # Определение значения time_value на основе даты приема
    if reception_date == datetime.today().astimezone(tz_utc_plus_5).date():
        time_value = f"{reception_time}"
        day = 'Сегодня'
        day_kz = 'бүгін '
    elif reception_date == (datetime.today().astimezone(tz_utc_plus_5) + timedelta(days=1)).date():
        time_value = f"{reception_time}"
        day = 'Завтра'
        day_kz = 'ертең'
    else:
        # Если префикс не подходит, пропускаем этот прием
        time_value = f"{reception_time}"
        day = ''
        day_kz = ''

    weekday_index = reception_date.weekday()  # 0 - понедельник, 6 - воскресенье

    weekday_map_ru = {
        0: "Понедельник", 1: "Вторник", 2: "Среду", 3: "Четверг",
        4: "Пятницу", 5: "Субботу", 6: "Воскресенье"
    }
    weekday_map_kz = {
        0: "Дүйсенбі", 1: "Сейсенбі", 2: "Сәрсенбі", 3: "Бейсенбі",
        4: "Жұма", 5: "Сенбі", 6: "Жексенбі"
    }

    weekday = weekday_map_ru.get(weekday_index, "")
    weekday_kz = weekday_map_kz.get(weekday_index, "")
    reception_day_data = get_formatted_date_info(reception_start_time)
    date = reception_day_data["date"]
    date_kz = reception_day_data["date_kz"]

    # Получаем данные о докторе и клинике из модели Appointment
    doctor_code = appointment.doctor.doctor_code if appointment.doctor else None
    specialist_name = appointment.doctor.full_name if appointment.doctor else ""
    clinic_id = appointment.clinic.clinic_id if appointment.clinic else None

    # Получаем специализацию
    specialization_id = appointment.doctor.specialization_id if appointment.doctor else None

    # Формирование данных для заказа
    order_list.append({
        "phone": appointment.patient.phone_mobile,
        "full_name": full_name,
        "info": {
            "time": time_value,
            "reception_id": appointment.appointment_id,
            "patient_code": appointment.patient.patient_code,
            "day": day,
            "day_kz": day_kz,
            "weekday": weekday,
            "weekday_kz": weekday_kz,
            "specialist_code": doctor_code,
            "specialization_id": specialization_id,
            "specialist_name": specialist_name,
            "clinic_id": clinic_id,
            "cabinet_number": appointment.cabinet_number,
            "service_id": appointment.service_id,
            "past_reception_start_time": reception_time_for_api,
            "original_time": time_value,
            "original_date": date,
            "original_date_kz": date_kz,
        }
    })

    return order_list, appointment.appointment_id
