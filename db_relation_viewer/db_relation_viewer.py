import os
import django
import pandas as pd
from tabulate import tabulate
from datetime import datetime

# Настройки Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'president_final.settings')
django.setup()

from reminder.models import Patient, Doctor, Clinic, Department, Appointment, QueueInfo


def format_date(date_obj):
    """Форматирует дату в читаемый вид"""
    if not date_obj:
        return "—"
    if isinstance(date_obj, datetime):
        return date_obj.strftime("%d.%m.%Y %H:%M")
    return date_obj.strftime("%d.%m.%Y")


class DBRelationshipViewer:
    """Класс для просмотра связей между сущностями в базе данных"""

    @staticmethod
    def show_patient_info(patient_code=None, full_name=None, limit=10):
        """Показать информацию о пациенте и его связи с другими сущностями"""

        query = Patient.objects.all()
        if patient_code:
            query = query.filter(patient_code=patient_code)
        if full_name:
            query = query.filter(full_name__icontains=full_name)

        patients = query.order_by('patient_code')[:limit]

        if not patients:
            print(f"❌ Пациенты не найдены.")
            return

        print(f"\n{'=' * 80}")
        print(f"ИНФОРМАЦИЯ О ПАЦИЕНТАХ (найдено: {len(patients)})")
        print(f"{'=' * 80}")

        for patient in patients:
            print(f"\n{'-' * 80}")
            print(f"👤 Пациент: {patient.full_name} (ID: {patient.patient_code})")
            print(f"📞 Телефон: {patient.phone_mobile or '—'}")
            print(f"📧 Email: {patient.email or '—'}")
            print(f"🎂 Дата рождения: {format_date(patient.birth_date) if patient.birth_date else '—'}")
            print(f"⚧ Пол: {'Мужской' if patient.gender == 1 else 'Женский' if patient.gender == 2 else '—'}")

            # Получаем активные записи к врачам
            appointments = Appointment.objects.filter(patient=patient, is_active=True).order_by('-start_time')
            if appointments:
                print(f"\n📅 ЗАПИСИ НА ПРИЕМ (активные):")
                for app in appointments[:5]:  # Показываем только 5 последних записей
                    doctor_name = app.doctor.full_name if app.doctor else "—"
                    clinic_name = app.clinic.name if app.clinic else "—"
                    dept_name = app.department.name if app.department else "—"
                    print(f"  • {format_date(app.start_time)} → {doctor_name} | {clinic_name} | {dept_name}")

                if len(appointments) > 5:
                    print(f"    ... и еще {len(appointments) - 5} записей")
            else:
                print("\n📅 Нет активных записей на прием")

            # Получаем информацию об очередях
            queues = QueueInfo.objects.filter(patient=patient).order_by('-created_at')
            if queues:
                print(f"\n🔄 ОЧЕРЕДИ:")
                for queue in queues[:3]:  # Показываем только 3 последние очереди
                    reason = queue.reason.reason_name if queue.reason else "—"
                    branch = queue.branch.name if queue.branch else "—"
                    target = queue.target_branch.name if queue.target_branch else "—"
                    status = f"{queue.current_state_name} ({queue.current_state})" if queue.current_state_name else "—"
                    print(f"  • Queue {queue.queue_id} | {reason} | {branch} → {target} | {status}")

                if len(queues) > 3:
                    print(f"    ... и еще {len(queues) - 3} очередей")
            else:
                print("\n🔄 Нет информации об очередях")

    @staticmethod
    def show_doctor_patients(doctor_code=None, doctor_name=None, limit=20):
        """Показать всех пациентов конкретного врача"""
        query = Doctor.objects.all()
        if doctor_code:
            query = query.filter(doctor_code=doctor_code)
        if doctor_name:
            query = query.filter(full_name__icontains=doctor_name)

        doctors = query.order_by('doctor_code')[:10]  # Ограничиваем до 10 врачей

        if not doctors:
            print(f"❌ Врачи не найдены.")
            return

        for doctor in doctors:
            print(f"\n{'=' * 80}")
            print(f"👨‍⚕️ ВРАЧ: {doctor.full_name} (ID: {doctor.doctor_code})")
            if doctor.department:
                print(f"🏢 Отделение: {doctor.department.name}")
            if doctor.clinic:
                print(f"🏥 Клиника: {doctor.clinic.name}")
            print(f"{'=' * 80}")

            # Находим всех пациентов врача через активные записи
            appointments = Appointment.objects.filter(doctor=doctor, is_active=True).order_by('-start_time')[:limit]

            if not appointments:
                print("Нет активных записей к этому врачу")
                continue

            print(f"\nПАЦИЕНТЫ С АКТИВНЫМИ ЗАПИСЯМИ (всего: {len(appointments)}):")
            for idx, app in enumerate(appointments, 1):
                date_str = format_date(app.start_time)
                print(f"{idx}. {app.patient.full_name} (ID: {app.patient.patient_code}) | {date_str}")

    @staticmethod
    def show_clinic_structure(clinic_id=None, clinic_name=None):
        """Показать структуру клиники: отделения и врачей"""
        query = Clinic.objects.all()
        if clinic_id:
            query = query.filter(clinic_id=clinic_id)
        if clinic_name:
            query = query.filter(name__icontains=clinic_name)

        clinics = query.order_by('clinic_id')[:5]  # Ограничиваем до 5 клиник

        if not clinics:
            print(f"❌ Клиники не найдены.")
            return

        for clinic in clinics:
            print(f"\n{'=' * 80}")
            print(f"🏥 КЛИНИКА: {clinic.name} (ID: {clinic.clinic_id})")
            print(f"📍 Адрес: {clinic.address or '—'}")
            print(f"📞 Телефон: {clinic.phone or '—'}")
            print(f"🕒 Часовой пояс: GMT+{clinic.timezone}")
            print(f"{'=' * 80}")

            # Получаем все отделения клиники
            departments = Department.objects.filter(clinic=clinic).order_by('name')

            if not departments:
                print("Нет информации об отделениях")
            else:
                print(f"\n🏢 ОТДЕЛЕНИЯ (всего: {departments.count()}):")

                for dept in departments:
                    print(f"\n{'-' * 70}")
                    print(f"  • {dept.name} (ID: {dept.department_id})")

                    # Получаем врачей этого отделения
                    doctors = Doctor.objects.filter(department=dept).order_by('full_name')

                    if doctors:
                        print(f"    👨‍⚕️ Врачи ({doctors.count()}):")
                        for doctor in doctors[:10]:  # Ограничиваем список до 10 врачей
                            print(f"      - {doctor.full_name} (ID: {doctor.doctor_code})")

                        if doctors.count() > 10:
                            print(f"        ... и еще {doctors.count() - 10} врачей")
                    else:
                        print(f"    👨‍⚕️ Нет привязанных врачей")

    @staticmethod
    def generate_summary_report():
        """Генерирует общую сводку по базе данных"""
        print(f"\n{'=' * 80}")
        print(f"СВОДНЫЙ ОТЧЕТ ПО БАЗЕ ДАННЫХ")
        print(f"{'=' * 80}")

        # Собираем статистику
        stats = {
            "Пациенты": Patient.objects.count(),
            "Врачи": Doctor.objects.count(),
            "Клиники": Clinic.objects.count(),
            "Отделения": Department.objects.count(),
            "Активные записи": Appointment.objects.filter(is_active=True).count(),
            "Очереди": QueueInfo.objects.count()
        }

        # Выводим основную статистику
        print("\nОбщая статистика:")
        for key, value in stats.items():
            print(f"{key}: {value}")

        # Статистика по клиникам
        print("\nРаспределение по клиникам:")
        clinics = Clinic.objects.all()

        clinic_data = []
        for clinic in clinics:
            doctors_count = Doctor.objects.filter(clinic=clinic).count()
            departments_count = Department.objects.filter(clinic=clinic).count()
            appointments_count = Appointment.objects.filter(clinic=clinic, is_active=True).count()

            clinic_data.append({
                "ID": clinic.clinic_id,
                "Название": clinic.name,
                "Отделения": departments_count,
                "Врачи": doctors_count,
                "Активные записи": appointments_count
            })

        # Создаем и выводим таблицу
        if clinic_data:
            df = pd.DataFrame(clinic_data)
            print(tabulate(df, headers='keys', tablefmt='grid', showindex=False))
        else:
            print("Нет данных о клиниках")


# Примеры использования
if __name__ == "__main__":
    viewer = DBRelationshipViewer()

    # Генерируем общий отчет
    viewer.generate_summary_report()

    # 1. Поиск пациента и его связей (по коду или имени)
    viewer.show_patient_info(patient_code=990000612)
    viewer.show_patient_info(full_name="Иванов")

    # 2. Показать пациентов конкретного врача
    # viewer.show_doctor_patients(doctor_code=123)
    # viewer.show_doctor_patients(doctor_name="Петров")

    # 3. Показать структуру клиники
    # viewer.show_clinic_structure(clinic_id=1)
    # viewer.show_clinic_structure(clinic_name="Центральная")
