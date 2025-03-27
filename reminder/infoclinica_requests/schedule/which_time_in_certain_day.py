import logging
logger = logging.getLogger(__name__)
import pytz
import logging

from datetime import datetime, timedelta
from django.http import JsonResponse
from reminder.infoclinica_requests.schedule.reserve_reception_for_patient import reserve_reception_for_patient
from reminder.models import *
from reminder.infoclinica_requests.utils import format_doctor_name, format_russian_date


def which_time_in_certain_day(patient_code, date_time):
    """
    Обработка запроса для получения доступных интервалов на определенный день.
    Возвращает все доступные слоты вместо только первых трех.
    """
    global doctor_name
    logger.info(
        f"Я в функции which_time_in_certain_day\nПришедшие данные: \npatient_code: {patient_code}\ndate_time: {date_time}")

    if len(date_time) == 10:
        date_time += " 00:00"

    try:
        date_time_obj = datetime.strptime(date_time, '%Y-%m-%d %H:%M')
    except ValueError as e:
        return JsonResponse({'status': 'error', 'message': f'Ошибка преобразования даты: {e}'}, status=400)

    patient = Patient.objects.get(patient_code=patient_code)

    appointment = Appointment.objects.filter(patient=patient, is_active=True).first()
    if appointment and appointment.doctor:
        doctor_code = appointment.doctor.doctor_code
        doctor_name = appointment.doctor.full_name
    else:
        # Если нет активных записей, пробуем получить из QueueInfo
        queue_entry = QueueInfo.objects.filter(patient=patient).first()
        if queue_entry and queue_entry.doctor_code:
            doctor_code = queue_entry.doctor_code
            doctor_name = queue_entry.doctor_name
        else:
            logger.info('Доктор и его код не найден')

    formatted_doc_name_final = format_doctor_name(doctor_name)

    patient_code = patient.patient_code
    trigger_id = 3

    # Получаем интервалы
    result_intervals = reserve_reception_for_patient(patient_code, date_time, trigger_id=trigger_id)
    logger.info(f"Полученные интервалы: {result_intervals}")

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

    # Если интервалы пусты
    if not result_intervals:
        formatted_date = format_russian_date(date_time_obj)
        return JsonResponse({
            'status': f'error_empty_windows_{response_status.split("_")[-1]}',
            'message': f'На дату {formatted_date} нет доступных окон.',
            'time_1': None,
            'time_2': None,
            'time_3': None,
            'day': day_for_return
        })

    # Обработка интервалов
    def extract_time(interval):
        """Преобразование строки интервала в формат времени"""
        if isinstance(interval, str):
            parts = interval.split(' ')  # Разделяем дату и время
            return parts[-1]  # Возвращаем только время
        return None

    # Формируем список всех доступных времен
    all_available_times = []

    for interval in result_intervals:
        time_value = extract_time(interval)
        if time_value:
            all_available_times.append(time_value)

    # Получаем также первые три времени для обратной совместимости
    first_time = extract_time(result_intervals[0]) if len(result_intervals) > 0 else None
    second_time = extract_time(result_intervals[1]) if len(result_intervals) > 1 else None
    third_time = extract_time(result_intervals[2]) if len(result_intervals) > 2 else None

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
        'time_1': first_time,
        'time_2': second_time,
        'time_3': third_time,
        'all_available_times': all_available_times,  # Добавляем полный список времен
        'date': formatted_date,
        'doctor': formatted_doc_name_final,
        'weekday': weekday,
        'day': day_for_return
    }

    return JsonResponse(response)


if __name__ == '__main__':
    which_time_in_certain_day(patient_code=990000612, date_time="2025-03-18")
