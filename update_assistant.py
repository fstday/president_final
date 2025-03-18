import os
import django
from openai import OpenAI
from django.conf import settings

# Настраиваем Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'president_final.settings')
django.setup()

# Импортируем необходимые инструменты из вашего кода
from reminder.openai_assistant.assistant_tools import TOOLS, create_assistant_with_tools
from reminder.models import Assistant

# Прописываем инструкции для ассистента
INSTRUCTIONS = """
# МЕДИЦИНСКИЙ АССИСТЕНТ ДЛЯ УПРАВЛЕНИЯ ЗАПИСЯМИ НА ПРИЕМ

Ты AI-ассистент для системы управления медицинскими записями, интегрированной с Infoclinica и голосовым роботом ACS. Твоя ГЛАВНАЯ задача - анализировать запросы пациентов на естественном языке, определять нужное действие, ВЫЗЫВАТЬ СООТВЕТСТВУЮЩУЮ ФУНКЦИЮ и форматировать ответ по требованиям системы.

## КРИТИЧЕСКИ ВАЖНО: ВСЕГДА ИСПОЛЬЗУЙ ФУНКЦИИ ВМЕСТО ТЕКСТОВЫХ ОТВЕТОВ

В следующих ситуациях ОБЯЗАТЕЛЬНО вызывай функцию, а НЕ отвечай текстом:

1. Когда пользователь спрашивает о свободных окошках или времени:
   ВСЕГДА вызывай функцию which_time_in_certain_day с параметрами reception_id и date_time

2. Когда пользователь интересуется своей текущей записью:
   ВСЕГДА вызывай функцию appointment_time_for_patient с параметром patient_code

3. Когда пользователь хочет записаться или ПЕРЕНЕСТИ запись:
   ВСЕГДА вызывай функцию reserve_reception_for_patient с параметрами patient_id, date_from_patient и trigger_id

4. Когда пользователь хочет отменить запись:
   ВСЕГДА вызывай функцию delete_reception_for_patient с параметром patient_id

НИКОГДА не отвечай текстом в этих ситуациях. Вместо этого ВСЕГДА вызывай соответствующую функцию с правильными параметрами.
"""


def main():
    print("Обновление ассистента OpenAI...")

    # Получаем API ключ из настроек Django
    api_key = settings.OPENAI_API_KEY
    if not api_key:
        print("❌ Ошибка: API ключ OpenAI не найден в настройках.")
        return

    # Создаем клиент OpenAI
    client = OpenAI(api_key=api_key)

    # Получаем текущий ассистент из БД или создаем новый
    try:
        db_assistant = Assistant.objects.first()
        assistant_id = db_assistant.assistant_id if db_assistant else None
        assistant_name = db_assistant.name if db_assistant else "Медицинский ассистент"

        # Создаем или обновляем ассистент с инструментами (функциями)
        if assistant_id:
            print(f"Обновляем существующего ассистента: {assistant_name} (ID: {assistant_id})")
            try:
                assistant = client.beta.assistants.update(
                    assistant_id=assistant_id,
                    name=assistant_name,
                    instructions=INSTRUCTIONS,
                    model="gpt-4-mini",  # можно поменять на другую модель
                    tools=TOOLS
                )
                print(f"✅ Ассистент успешно обновлен с функциями.")
            except Exception as e:
                print(f"❌ Ошибка при обновлении ассистента: {e}")
                print("Создаем нового ассистента...")
                assistant = create_assistant_with_tools(
                    client=client,
                    name=assistant_name,
                    instructions=INSTRUCTIONS,
                    model="gpt-4-mini"
                )

                # Обновляем или создаем запись в БД
                if db_assistant:
                    db_assistant.assistant_id = assistant.id
                    db_assistant.instructions = INSTRUCTIONS
                    db_assistant.model = "gpt-4-mini"
                    db_assistant.save()
                else:
                    Assistant.objects.create(
                        assistant_id=assistant.id,
                        name=assistant_name,
                        instructions=INSTRUCTIONS,
                        model="gpt-4-mini"
                    )
                print(f"✅ Создан новый ассистент с ID: {assistant.id}")
        else:
            print(f"Создаем нового ассистента: {assistant_name}")
            assistant = create_assistant_with_tools(
                client=client,
                name=assistant_name,
                instructions=INSTRUCTIONS,
                model="gpt-4-mini"
            )

            # Сохраняем в БД
            Assistant.objects.create(
                assistant_id=assistant.id,
                name=assistant_name,
                instructions=INSTRUCTIONS,
                model="gpt-4-mini"
            )
            print(f"✅ Создан новый ассистент с ID: {assistant.id}")

        print("\nФункции ассистента:")
        for tool in TOOLS:
            print(f"- {tool['function']['name']}")

    except Exception as e:
        print(f"❌ Произошла ошибка: {e}")


if __name__ == "__main__":
    main()
