import pika
import json
import redis
import psycopg2
import os
import time
from datetime import datetime
import pytz

redis_client = redis.Redis(
    host=os.environ.get('REDIS_HOST', 'redis'),
    port=int(os.environ.get('REDIS_PORT', 6379)),
    decode_responses=True
)

def get_db_connection():
    return psycopg2.connect(
        host=os.environ.get('POSTGRES_HOST', 'haproxy'),
        port=int(os.environ.get('POSTGRES_PORT', 26256)),
        user=os.environ.get('POSTGRES_USER', 'root'),
        password=os.environ.get('POSTGRES_PASSWORD', ''),
        database=os.environ.get('POSTGRES_DB', 'appdb')
    )

def connect_to_cockroach():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT now()')
        result = cursor.fetchone()
        print(f"✅ Connected to CockroachDB via HAProxy")
        print(f"🕒 Current time: {result[0]}")
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"❌ Connection error: {str(e)}")

def connect_to_rabbit_with_retry(max_retries=20, delay=5):
    last_error = None
    print(f"🔄 Tentando conectar ao RabbitMQ (max {max_retries} tentativas)...")
    
    for attempt in range(1, max_retries + 1):
        try:
            print(f"🔄 Tentativa {attempt}/{max_retries} de conexão ao RabbitMQ")
            connection = pika.BlockingConnection(
                pika.ConnectionParameters(
                    host='rabbitmq',
                    heartbeat=600,
                    blocked_connection_timeout=300
                )
            )
            channel = connection.channel()
            channel.queue_declare(queue='add_key', durable=True)
            channel.queue_declare(queue='del_key', durable=True)
            print("✅ Conectado ao RabbitMQ com sucesso!")
            return channel, connection
        except Exception as e:
            last_error = e
            print(f"⚠️ RabbitMQ não está pronto (tentativa {attempt}/{max_retries}): {str(e)}")
            if attempt == max_retries:
                print(f"❌ Falha ao conectar ao RabbitMQ após {max_retries} tentativas")
                raise Exception(f"❌ Não foi possível conectar ao RabbitMQ: {str(e)}")
            time.sleep(delay)

def process_add_key(ch, method, properties, body):
    try:
        data = json.loads(body)
        key_name = data.get('key_name')
        key_value = data.get('key_value')
        timestamp = data.get('timestamp')
        
        if not all([key_name, key_value, timestamp]):
            print(f"⚠️ Invalid add_key message: {body}")
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            return
            
        # Garantir que a data tem timezone (UTC)
        ts = datetime.fromisoformat(timestamp)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=pytz.UTC)
        
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO kv_store (key, value, last_updated)
            VALUES (%s, %s, %s)
            ON CONFLICT (key)
            DO UPDATE SET value = %s, last_updated = %s
            WHERE kv_store.last_updated <= %s
            """,
            [key_name, key_value, ts, key_value, ts, ts]
        )
        
        rowcount = cursor.rowcount
        conn.commit()
        cursor.close()
        conn.close()
        
        if rowcount > 0:
            try:
                redis_val = redis_client.get(key_name)
                if redis_val is not None:
                    redis_client.set(key_name, key_value)
            except Exception as e:
                print(f"❌ Redis Error: {str(e)}")
                
        print(f"✅ [add_key] {key_name} set to \"{key_value}\" at {timestamp}")
        ch.basic_ack(delivery_tag=method.delivery_tag)
    except Exception as e:
        print(f"❌ [add_key] Error: {str(e)}")
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

def process_del_key(ch, method, properties, body):
    try:
        data = json.loads(body)
        key_name = data.get('key_name')
        timestamp = data.get('timestamp')
        
        if not all([key_name, timestamp]):
            print(f"⚠️ Invalid del_key message: {body}")
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            return
            
        # Garantir que a data tem timezone (UTC)
        ts = datetime.fromisoformat(timestamp)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=pytz.UTC)
        
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT last_updated FROM kv_store WHERE key = %s', [key_name])
        result = cursor.fetchone()
        
        if not result:
            retries = 0
            if properties.headers and 'x-retry' in properties.headers:
                retries = properties.headers['x-retry']
                
            if retries < 3:
                print(f"⏳ [del_key] Key \"{key_name}\" not found. Retrying... (attempt {retries + 1})")
                ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
                
                headers = {'x-retry': retries + 1}
                ch.basic_publish(
                    exchange='',
                    routing_key='del_key',
                    body=body,
                    properties=pika.BasicProperties(
                        headers=headers,
                        expiration='3000'
                    )
                )
                return
            else:
                print(f"❌ [del_key] Key \"{key_name}\" not found after {retries} retries. Dropping.")
                ch.basic_ack(delivery_tag=method.delivery_tag)
                return
                
        db_timestamp = result[0]
        
        if db_timestamp <= ts:
            cursor.execute('DELETE FROM kv_store WHERE key = %s', [key_name])
            conn.commit()
            print(f"✅ [del_key] Deleted \"{key_name}\" at {timestamp}")
            
            try:
                redis_val = redis_client.get(key_name)
                if redis_val is not None:
                    redis_client.delete(key_name)
            except Exception as e:
                print(f"❌ Redis Error: {str(e)}")
        else:
            print(f"⏩ [del_key] Skipped deletion of \"{key_name}\" — newer value exists.")
            
        cursor.close()
        conn.close()
        ch.basic_ack(delivery_tag=method.delivery_tag)
    except Exception as e:
        print(f"❌ [del_key] Error: {str(e)}")
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

def main():
    print("🚀 Iniciando consumidor...")
    connect_to_cockroach()
    
    # Esperar um pouco antes de tentar conectar ao RabbitMQ
    print("⏳ Aguardando 5 segundos para o RabbitMQ estar disponível...")
    time.sleep(5)
    
    channel, connection = connect_to_rabbit_with_retry()
    
    print('📬 Escutando filas add_key e del_key...')
    channel.basic_consume(queue='add_key', on_message_callback=process_add_key)
    channel.basic_consume(queue='del_key', on_message_callback=process_del_key)
    
    try:
        channel.start_consuming()
    except KeyboardInterrupt:
        channel.stop_consuming()
    finally:
        if connection.is_open:
            connection.close()
            print("🔌 Conexão com RabbitMQ fechada")

if __name__ == "__main__":
    main() 