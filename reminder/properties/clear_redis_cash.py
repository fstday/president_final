import redis

redis_client = redis.Redis(host='localhost', port=6379, db=0)


def redis_client_flush_db():
    redis_client.flushdb()
    print("База данных Redis очищена")


if __name__ == "__main__":
    redis_client_flush_db()
