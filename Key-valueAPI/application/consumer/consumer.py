import pika
import json
import redis
import psycopg2
import os
import time
import socket
from datetime import datetime
import pytz

# Configuração para Redis Sentinel se estiver habilitado
if os.environ.get('REDIS_USE_SENTINEL', 'false').lower() == 'true':
    import redis.sentinel
    sentinel_hosts = os.environ.get('SENTINEL_HOSTS', 'sentinel-1:26379').split(',')
    sentinel_list = [tuple(host.split(':')) for host in sentinel_hosts]
    sentinel = redis.sentinel.Sentinel(sentinel_list)
    redis_client = sentinel.master_for(
        os.environ.get('REDIS_MASTER_NAME', 'mymaster'),
        decode_responses=True
    )
else:
    redis_client = redis.Redis(
        host=os.environ.get('REDIS_HOST', 'redis-master'),
        port=int(os.environ.get('REDIS_PORT', 6379)),
        decode_responses=True
    )

# Prefetch count - controla quantas mensagens cada consumidor pode processar de uma vez
PREFETCH_COUNT = int(os.environ.get("PREFETCH_COUNT", 20))
CONSUMER_NAME = os.environ.get("CONSUMER_NAME", socket.gethostname())

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
        print(f"Connected to CockroachDB: {result[0]}")
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"Connection error: {str(e)}")

def connect_to_rabbit_with_retry(max_retries=20, delay=5):
    last_error = None
    # Usar uma URL completa para conexão com RabbitMQ
    rabbit_url = os.environ.get('RABBIT_URL', 'amqp://appuser:s3nh4segura@broker-1:5672/')
    
    print(f"Connecting to RabbitMQ: {rabbit_url} (max {max_retries} attempts)...")
    
    for attempt in range(1, max_retries + 1):
        try:
            print(f"Attempt {attempt}/{max_retries} to connect to RabbitMQ")
            connection_params = pika.URLParameters(rabbit_url)
            connection_params.heartbeat = 600
            connection_params.blocked_connection_timeout = 300
            connection = pika.BlockingConnection(connection_params)
            
            channel = connection.channel()
            channel.queue_declare(queue='add_key', durable=True)
            channel.queue_declare(queue='del_key', durable=True)
            # Configurar prefetch count para processar várias mensagens simultaneamente
            channel.basic_qos(prefetch_count=PREFETCH_COUNT)
            print(f"Connected to RabbitMQ (prefetch: {PREFETCH_COUNT})")
            return channel, connection
        except Exception as e:
            last_error = e
            print(f"RabbitMQ not ready (attempt {attempt}/{max_retries}): {str(e)}")
            if attempt == max_retries:
                print(f"Failed to connect to RabbitMQ after {max_retries} attempts")
                # Tentativa final: conectar usando localhost
                try:
                    print(f"Trying localhost connection...")
                    connection = pika.BlockingConnection(
                        pika.URLParameters('amqp://appuser:s3nh4segura@localhost:5672/')
                    )
                    channel = connection.channel()
                    channel.queue_declare(queue='add_key', durable=True)
                    channel.queue_declare(queue='del_key', durable=True)
                    # Configurar prefetch count para processar várias mensagens simultaneamente
                    channel.basic_qos(prefetch_count=PREFETCH_COUNT)
                    print(f"Connected to RabbitMQ via localhost (prefetch: {PREFETCH_COUNT})")
                    return channel, connection
                except Exception as local_err:
                    print(f"Localhost connection failed: {str(local_err)}")
                
                raise Exception(f"Cannot connect to RabbitMQ: {str(e)}")
            time.sleep(delay)

def process_add_key(ch, method, properties, body):
    try:
        data = json.loads(body)
        key_name = data.get('key_name')
        key_value = data.get('key_value')
        timestamp = data.get('timestamp')
        
        if not all([key_name, key_value, timestamp]):
            print(f"Invalid add_key message: {body}")
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
                # Para operações PUT, não atualizamos o cache (seguindo modelo referência)
                # Se existir no cache, removemos para forçar um fetch do banco de dados
                if redis_client.exists(key_name):
                    redis_client.delete(key_name)
                    redis_client.delete(f"accessed:{key_name}")
            except Exception as e:
                print(f"Redis Error: {str(e)}")
                
        print(f"[add_key] {key_name} set to \"{key_value}\" at {timestamp}")
        ch.basic_ack(delivery_tag=method.delivery_tag)
    except Exception as e:
        print(f"[add_key] Error: {str(e)}")
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

def process_del_key(ch, method, properties, body):
    try:
        data = json.loads(body)
        key_name = data.get('key_name')
        timestamp = data.get('timestamp')
        
        if not all([key_name, timestamp]):
            print(f"Invalid del_key message: {body}")
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
                print(f"[del_key] Key \"{key_name}\" not found. Retrying... (attempt {retries + 1})")
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
                print(f"[del_key] Key \"{key_name}\" not found after {retries} retries. Dropping.")
                ch.basic_ack(delivery_tag=method.delivery_tag)
                return
                
        db_timestamp = result[0]
        
        if db_timestamp <= ts:
            cursor.execute('DELETE FROM kv_store WHERE key = %s', [key_name])
            conn.commit()
            print(f"[del_key] Deleted \"{key_name}\" at {timestamp}")
            
            try:
                # Remover do cache tanto o valor quanto a flag de acesso (seguindo modelo referência)
                redis_client.delete(key_name)
                redis_client.delete(f"accessed:{key_name}")
            except Exception as e:
                print(f"Redis Error: {str(e)}")
        else:
            print(f"[del_key] Skipped deletion of \"{key_name}\" — newer value exists.")
            
        cursor.close()
        conn.close()
        ch.basic_ack(delivery_tag=method.delivery_tag)
    except Exception as e:
        print(f"[del_key] Error: {str(e)}")
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

def main():
    print(f"Starting consumer {CONSUMER_NAME}...")
    connect_to_cockroach()
    
    # Esperar um pouco antes de tentar conectar ao RabbitMQ
    print("Waiting 5 seconds for RabbitMQ...")
    time.sleep(5)
    
    # Loop principal com retry automático
    while True:
        try:
            # Tentar conectar usando a URL configurada
            print("Connecting to RabbitMQ...")
            channel, connection = connect_to_rabbit_with_retry()
            
            print(f'Consumer {CONSUMER_NAME} listening to queues add_key and del_key (prefetch: {PREFETCH_COUNT})...')
            channel.basic_consume(queue='add_key', on_message_callback=process_add_key)
            channel.basic_consume(queue='del_key', on_message_callback=process_del_key)
            
            channel.start_consuming()
        except KeyboardInterrupt:
            if 'channel' in locals() and channel:
                channel.stop_consuming()
            break
        except Exception as e:
            print(f"Error: {str(e)} - Reconnecting in 5 seconds...")
            time.sleep(5)
        finally:
            if 'connection' in locals() and connection and connection.is_open:
                connection.close()
                print("Connection to RabbitMQ closed")

if __name__ == "__main__":
    main() 