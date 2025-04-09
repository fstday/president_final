import os
from datetime import datetime, timedelta

import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'president_final.settings')
django.setup()

import requests
import json
from django.utils.timezone import now
from reminder.properties.utils import ACS_BASE_URL, get_latest_api_key, get_formatted_date_info
from reminder.models import Appointment, Call, QueueInfo, Patient


def process_queue_to_acs():
    """
    Обрабатывает активные записи в очереди и отправляет их в ACS систему.
    Использует метод отправки с нужной структурой полей.
    """
    api_key = get_latest_api_key()
    if not api_key:
        print("Не удалось получить API ключ ACS")
        return False

    active_queue_entries = QueueInfo.objects.all().select_related('patient', 'target_branch').order_by('-created_at')
    print(f"Найдено {active_queue_entries.count()} активных записей в очереди")

    success_count = 0
    error_count = 0

    for queue_entry in active_queue_entries:
        patient = queue_entry.patient
        if not patient:
            print(f"⚠ Очередь {queue_entry.queue_id} не имеет пациента, пропускаем.")
            error_count += 1
            continue

        if not patient.phone_mobile:
            print(f"⚠ Пациент {patient.patient_code} не имеет номера телефона, пропускаем.")
            error_count += 1
            continue

        queue_reason_code = (
                queue_entry.internal_reason_code or
                getattr(patient, 'last_queue_reason_code', None) or
                "00PP0consulta"
        )

        print(f"✅ Используем причину: {queue_reason_code}")

        # Обработка номера телефона
        phone = ''.join(filter(str.isdigit, patient.phone_mobile))
        if phone.startswith('8'):
            phone = '7' + phone[1:]
        elif not phone.startswith('7'):
            phone = '7' + phone

        # Базовые данные приема
        doctor_name = "Не назначен"
        clinic_name = ""
        clinic_address = ""
        department_name = ""
        appointment_date = "Не назначено"
        appointment_time = "Не назначено"
        appointment_id = None
        doctor_code = None
        specialization_id = None
        cabinet_number = None
        service_id = None
        weekday = ""
        weekday_kz = ""
        date = ""
        date_kz = ""
        relation = None

        try:
            # Поиск связанного приема
            appointment = Appointment.objects.filter(
                patient=patient,
                is_active=True,
                start_time__gt=now()
            ).order_by('start_time').first()

            if appointment:
                doctor_name = str(appointment.doctor.full_name) if appointment.doctor else "Не назначен"
                clinic_name = str(appointment.clinic.name) if appointment.clinic else ""
                clinic_address = str(appointment.clinic.address) if appointment.clinic else ""
                department_name = str(appointment.department.name) if appointment.department else ""

                # Преобразование времени приема в нужный формат
                reception_start_time = appointment.start_time
                date_object = reception_start_time

                # Определяем отношение к текущему дню (сегодня/завтра)
                today = datetime.now().date()
                tomorrow = today + timedelta(days=1)
                reception_date = reception_start_time.date()

                if reception_date == today:
                    relation = "today"
                elif reception_date == tomorrow:
                    relation = "tomorrow"

                # Форматирование даты и времени
                appointment_date = reception_start_time.strftime("%d.%m.%Y")
                appointment_time = reception_start_time.strftime("%H:%M")
                appointment_id = appointment.appointment_id
                reception_time_for_api = reception_start_time.strftime('%Y-%m-%d %H:%M')

                # Получаем код доктора и специализацию
                if appointment.doctor:
                    doctor_code = appointment.doctor.doctor_code
                    specialization_id = appointment.doctor.specialization_id

                # Получаем дополнительные данные
                cabinet_number = appointment.cabinet_number
                service_id = appointment.service_id

                # Получаем информацию о дне недели
                weekday_index = reception_date.weekday()
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

                # Форматируем дату
                reception_day_data = get_formatted_date_info(
                    reception_start_time) if 'get_formatted_date_info' in globals() else {"date": "", "date_kz": ""}
                date = reception_day_data.get("date", "")
                date_kz = reception_day_data.get("date_kz", "")

            elif queue_entry.target_branch:
                clinic_name = str(queue_entry.target_branch.name)
                clinic_address = str(queue_entry.target_branch.address or "")
        except Exception as e:
            print(f"⚠ Ошибка при получении данных о приеме: {e}")

        # Формирование данных в нужном формате
        json_data = {
            "phone": phone,
            "full_name": str(patient.get_full_name() or ""),
            "info": {
                "time": appointment_time,
                "reception_id": appointment_id,
                "patient_code": patient.patient_code,
                "day": "сегодня" if relation == "today" else "завтра" if relation == "tomorrow" else "",
                "day_kz": "бүгін" if relation == "today" else "ертең" if relation == "tomorrow" else "",
                "weekday": weekday,
                "weekday_kz": weekday_kz,
                "specialist_code": doctor_code,
                "specialization_id": specialization_id,
                "specialist_name": doctor_name,
                "clinic_id": queue_entry.target_branch.clinic_id if queue_entry.target_branch else None,
                "cabinet_number": cabinet_number,
                "service_id": service_id,
                "past_reception_start_time": reception_time_for_api if 'reception_time_for_api' in locals() else "",
                "original_time": appointment_time,
                "original_date": date,
                "original_date_kz": date_kz,
            }
        }

        print("Отправка данных в формате JSON:")
        print(json.dumps(json_data, indent=2, ensure_ascii=False))

        url = f"{ACS_BASE_URL}/api/v2/bpm/public/bp/{api_key}/add_orders"

        try:
            print(f"Отправка заказа в ACS для пациента {patient.patient_code} с причиной {queue_reason_code}")
            # Отправляем как плоскую JSON структуру
            headers = {'Content-Type': 'application/json'}
            response = requests.post(url, json=json_data, headers=headers)

            print(f"Ответ сервера: {response.status_code}")
            if response.text:
                print(f"Текст ответа: {response.text[:200]}...")  # Печатаем первые 200 символов

            if response.status_code == 200:
                # Обработка успешного ответа и сохранение данных в БД
                # ... (остальной код обработки ответа остается без изменений)
                try:
                    result_data = response.json()
                    print(f"Структура ответа: {type(result_data).__name__}")

                    # Извлечение order_key в зависимости от формата ответа
                    order_key = None

                    # Проверка формата ответа и корректное извлечение order_key
                    if isinstance(result_data, dict):
                        if 'data' in result_data and phone in result_data.get('data', {}):
                            phone_data = result_data.get('data', {}).get(phone, {})
                            if isinstance(phone_data, dict) and 'order' in phone_data:
                                order_key = phone_data.get('order')
                        elif 'order' in result_data:
                            order_key = result_data.get('order')
                    elif isinstance(result_data, list):
                        for item in result_data:
                            if isinstance(item, dict) and 'order' in item:
                                order_key = item.get('order')
                                break

                    print(f"Извлеченный order_key: {order_key}")

                    if order_key:
                        # Сохранение данных в БД
                        # ... (остальной код работы с БД остается без изменений)
                        try:
                            # Создание или обновление записи звонка
                            if appointment:
                                call, created = Call.objects.get_or_create(
                                    appointment=appointment,
                                    call_type="queue",
                                    defaults={
                                        "order_key": order_key,
                                        "queue_id": queue_entry.queue_id,
                                        "patient_code": patient.patient_code
                                    }
                                )
                            else:
                                call, created = Call.objects.get_or_create(
                                    queue_id=queue_entry.queue_id,
                                    patient_code=patient.patient_code,
                                    call_type="queue",
                                    defaults={"order_key": order_key}
                                )

                            if created:
                                print(f"✅ Создан звонок для {patient.get_full_name()} очередь {queue_entry.queue_id}")
                                success_count += 1
                            else:
                                if call.order_key != order_key:
                                    call.order_key = order_key
                                    call.save(update_fields=['order_key'])
                                    print(f"🔄 Обновлен order_key для {patient.get_full_name()}")
                                success_count += 1
                        except Exception as e:
                            print(f"❌ Ошибка при создании записи звонка: {e}")
                            error_count += 1
                    else:
                        print(f"❌ Нет order_key в ответе для телефона {phone}")
                        error_count += 1
                except Exception as e:
                    print(f"❌ Ошибка при разборе ответа: {e}")
                    error_count += 1
            else:
                print(f"❌ Ошибка при отправке в ACS: {response.status_code} - {response.text}")
                error_count += 1

        except Exception as e:
            print(f"❌ Ошибка запроса ACS для пациента {patient.patient_code}: {e}")
            error_count += 1

    print(f"Обработка завершена. Успешно: {success_count}, Ошибок: {error_count}")
    return success_count > 0


if __name__ == "__main__":
    process_queue_to_acs()
