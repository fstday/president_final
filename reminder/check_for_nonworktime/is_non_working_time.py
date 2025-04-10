import logging
from datetime import datetime


logger = logging.getLogger(__name__)


def is_non_working_time(time_str: str) -> bool:
    try:
        time_obj = datetime.strptime(time_str, "%H:%M").time()
        start_time = datetime.strptime("09:00", "%H:%M").time()
        end_time = datetime.strptime("20:30", "%H:%M").time()
        logger.info(f"[DEBUG] Сравнение времени: {time_obj} между {start_time} и {end_time}")
        return not (start_time <= time_obj <= end_time)
    except ValueError as e:
        logger.info(f"[ERROR] Ошибка парсинга времени: {time_str} ({e})")
        return True
