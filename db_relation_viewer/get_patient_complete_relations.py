import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'president_final.settings')
django.setup()

from django.db.models import Q, F
from reminder.models import Patient


def get_patient_complete_relations(patient_code):
    """
    Получает все связи пациента с различными сущностями.

    Args:
        patient_code (int): Код пациента

    Returns:
        dict: Словарь со всеми связями пациента
    """
    try:
        patient = Patient.objects.get(patient_code=patient_code)
    except Patient.DoesNotExist:
        return {"error": f"Пациент с кодом {patient_code} не найден"}

    # Получаем все записи на прием пациента с их связями
    appointments = patient.appointments.select_related(
        'doctor',
        'clinic',
        'department',
        'reason'
    ).all()

    # Сбор информации по отделениям и филиалам
    departments_data = []
    clinics_data = []

    for appointment in appointments:
        # Отделение
        if appointment.department:
            dept_info = {
                'id': appointment.department.department_id,
                'name': appointment.department.name,
                'clinic_id': appointment.department.clinic.clinic_id if appointment.department.clinic else None,
                'clinic_name': appointment.department.clinic.name if appointment.department.clinic else None
            }
            # Избегаем дублирования
            if dept_info not in departments_data:
                departments_data.append(dept_info)

        # Филиал
        if appointment.clinic:
            clinic_info = {
                'id': appointment.clinic.clinic_id,
                'name': appointment.clinic.name,
                'address': appointment.clinic.address,
                'phone': appointment.clinic.phone,
                'email': appointment.clinic.email
            }
            # Избегаем дублирования
            if clinic_info not in clinics_data:
                clinics_data.append(clinic_info)

    # Получаем информацию о врачах
    doctors = []
    for appointment in appointments:
        if appointment.doctor:
            doc_info = {
                'id': appointment.doctor.doctor_code,
                'name': appointment.doctor.full_name,
                'specialization': appointment.doctor.specialization,
                'specialization_id': appointment.doctor.specialization_id,
                'department_id': appointment.doctor.department.department_id if appointment.doctor.department else None,
                'clinic_id': appointment.doctor.clinic.clinic_id if appointment.doctor.clinic else None
            }
            if doc_info not in doctors:
                doctors.append(doc_info)

    # Получаем информацию об очередях
    queue_entries = patient.queue_entries.select_related(
        'appointment',
        'reason',
        'branch',
        'target_branch'
    ).all()

    queues_data = []
    for queue in queue_entries:
        queue_info = {
            'id': queue.queue_id,
            'current_state': queue.current_state,
            'current_state_name': queue.current_state_name,
            'reason_id': queue.reason.reason_id if queue.reason else None,
            'reason_name': queue.reason.reason_name if queue.reason else None,
            'source_branch_id': queue.branch.clinic_id if queue.branch else None,
            'source_branch_name': queue.branch.name if queue.branch else None,
            'target_branch_id': queue.target_branch.clinic_id if queue.target_branch else None,
            'target_branch_name': queue.target_branch.name if queue.target_branch else None
        }
        queues_data.append(queue_info)

    # Получаем звонки
    calls = []
    for appointment in appointments:
        for call in appointment.calls.all():
            call_info = {
                'order_key': call.order_key,
                'status_id': call.status_id,
                'call_type': call.call_type,
                'queue_id': call.queue_id
            }
            calls.append(call_info)

    # Оформляем сводную информацию
    result = {
        'patient': {
            'code': patient.patient_code,
            'full_name': patient.full_name,
            'first_name': patient.first_name,
            'last_name': patient.last_name,
            'middle_name': patient.middle_name,
            'phone_mobile': patient.phone_mobile,
            'email': patient.email,
            'birth_date': patient.birth_date.isoformat() if patient.birth_date else None,
            'gender': patient.get_gender_display() if patient.gender else None,
            'last_queue_reason_code': patient.last_queue_reason_code,
            'last_queue_reason_name': patient.last_queue_reason_name
        },
        'departments': departments_data,
        'clinics': clinics_data,
        'doctors': doctors,
        'appointments': [
            {
                'id': app.appointment_id,
                'start_time': app.start_time.isoformat(),
                'end_time': app.end_time.isoformat() if app.end_time else None,
                'status': app.status,
                'service_name': app.service_name,
                'reason_id': app.reason.reason_id if app.reason else None,
                'reason_name': app.reason.reason_name if app.reason else None,
                'doctor_id': app.doctor.doctor_code if app.doctor else None,
                'doctor_name': app.doctor.full_name if app.doctor else None,
                'department_id': app.department.department_id if app.department else None,
                'department_name': app.department.name if app.department else None,
                'clinic_id': app.clinic.clinic_id if app.clinic else None,
                'clinic_name': app.clinic.name if app.clinic else None
            }
            for app in appointments
        ],
        'queues': queues_data,
        'calls': calls
    }

    return result


# Пример использования:
def example_usage():
    patient_code = 990000612  # Код вашего пациента
    patient_relations = get_patient_complete_relations(patient_code)

    # Выводим информацию о филиалах и отделениях
    print("=== Филиалы, с которыми взаимодействовал пациент ===")
    for clinic in patient_relations['clinics']:
        print(f"ID: {clinic['id']}, Название: {clinic['name']}")

    print("\n=== Отделения, с которыми взаимодействовал пациент ===")
    for dept in patient_relations['departments']:
        print(f"ID: {dept['id']}, Название: {dept['name']}, Филиал ID: {dept['clinic_id']}")


if __name__ == '__main__':
    example_usage()
