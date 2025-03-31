from django.urls import path
from reminder.openai_assistant.api_views import (
    process_voicebot_request,
    get_assistant_info
)

urlpatterns = [
    # Основной эндпоинт для обработки запросов от голосового робота
    path('voicebot/infoclinica-clinic/v1/', process_voicebot_request, name='voicebot_endpoint'),

    # Административные эндпоинты для управления ассистентами
    path('api/assistants/info/', get_assistant_info, name='assistant_info'),
]
