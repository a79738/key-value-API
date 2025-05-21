import pika
import redis
import psycopg2
import json
import os
import time
import logging
from datetime import datetime, timezone

# Configuração de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Conexão Redis
redis_client = redis.Redis(
    host=os.getenv('REDIS_HOST', 'redis'),
    port=int(os.getenv('REDIS_PORT', 6379))
)

# Conexão PostgreSQL
pg_conn = psycopg2.connect(
    host=os.getenv('POSTGRES_HOST', 'haproxy'),
    port=int(os.getenv('POSTGRES_PORT', 26256)),
    user=os.getenv('POSTGRES_USER', 'root'),
    password=os.getenv('POSTGRES_PASSWORD', ''),
    database=os.getenv('POSTGRES_DB', 'appdb')
)

# Verificando conexão com o banco de dados
with pg_conn.cursor() as cur:
    cur.execute("SELECT now()")
    result = cur.fetchone()
    logger.info(f"🕒 Horário atual: {result[0]}")
    
    cur.execute("""
        SELECT column_name, data_type 
        FROM information_schema.columns 
        WHERE table_name = 'kv_store'
    """)
    logger.info("📊 Estrutura da tabela:")
    for row in cur.fetchall():
        logger.info(f"  - {row[0]}: {row[1]}")

# Conexão RabbitMQ
def connect_to_rabbitmq():
    rabbitmq_host = os.getenv('RABBITMQ_HOST', 'rabbitmq')
    for attempt in range(1, 11):
        try:
            connection = pika.BlockingConnection(
                pika.ConnectionParameters(host=rabbitmq_host)
            )
            channel = connection.channel()
            channel.queue_declare(queue='add_key', durable=True)
            channel.queue_declare(queue='del_key', durable=True)
            logger.info(f"✅ Conectado ao RabbitMQ: {rabbitmq_host}")
            return connection, channel
        except pika.exceptions.AMQPConnectionError:
            logger.warning(f"RabbitMQ ({rabbitmq_host}) não está pronto. Tentativa {attempt}/10. Tentando novamente em 2 segundos...")
            time.sleep(2)
    
    raise Exception(f"Não foi possível conectar ao RabbitMQ ({rabbitmq_host}) após várias tentativas")

# Conecta ao RabbitMQ
rabbitmq_conn, rabbitmq_channel = connect_to_rabbitmq()

def handle_add_key(ch, method, properties, body):
    try:
        # Decodifica a mensagem
        message = json.loads(body.decode())
        key_name = message.get('key_name')
        key_value = message.get('key_value')
        timestamp = message.get('timestamp')
        
        if not key_name or not key_value or not timestamp:
            logger.warning(f"⚠️ Mensagem inválida para add_key: {body.decode()}")
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            return
        
        # Converte o timestamp para objeto datetime com timezone UTC
        ts = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        
        logger.info(f"📝 Inserindo/atualizando chave \"{key_name}\" com valor \"{key_value}\"")
        
        # Executa a operação no banco de dados
        with pg_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO kv_store (key, value, last_updated)
                VALUES (%s, %s, %s)
                ON CONFLICT (key)
                DO UPDATE SET value = %s, last_updated = %s
                WHERE kv_store.last_updated <= %s
                RETURNING key, value, last_updated
                """,
                (key_name, key_value, ts, key_value, ts, ts)
            )
            pg_conn.commit()
            
            if cur.rowcount == 0:
                logger.info(f"⏩ Atualização ignorada para \"{key_name}\" — valor mais recente já existe")
            else:
                logger.info(f"✅ [add_key] {key_name} definido como \"{key_value}\" em {timestamp}")
        
        # Confirma o processamento da mensagem
        ch.basic_ack(delivery_tag=method.delivery_tag)
        
    except Exception as e:
        logger.error(f"❌ [add_key] Erro: {str(e)}")
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

def handle_del_key(ch, method, properties, body):
    try:
        # Decodifica a mensagem
        message = json.loads(body.decode())
        key_name = message.get('key_name')
        timestamp = message.get('timestamp')
        
        if not key_name or not timestamp:
            logger.warning(f"⚠️ Mensagem inválida para del_key: {body.decode()}")
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            return
        
        # Converte o timestamp para objeto datetime com timezone UTC
        ts = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        
        # Verifica se a chave existe no banco de dados
        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT last_updated FROM kv_store WHERE key = %s",
                (key_name,)
            )
            result = cur.fetchone()
            
            if not result:
                # Verifica se é uma tentativa de retry
                retry_count = 0
                if properties.headers and 'x-retry' in properties.headers:
                    retry_count = properties.headers['x-retry']
                
                if retry_count < 3:
                    logger.warning(f"⏳ [del_key] Chave \"{key_name}\" não encontrada. Tentando novamente... (tentativa {retry_count + 1})")
                    
                    # Cria nova mensagem com contador de retry incrementado
                    headers = {'x-retry': retry_count + 1}
                    ch.basic_publish(
                        exchange='',
                        routing_key='del_key',
                        body=body,
                        properties=pika.BasicProperties(
                            headers=headers,
                            expiration='3000'
                        )
                    )
                    ch.basic_ack(delivery_tag=method.delivery_tag)
                    return
                else:
                    logger.warning(f"❌ [del_key] Chave \"{key_name}\" não encontrada após {retry_count} tentativas. Descartando.")
                    ch.basic_ack(delivery_tag=method.delivery_tag)
                    return
            
            # Verifica timestamp - o db_timestamp já vem com timezone
            db_timestamp = result[0]
            logger.info(f"Comparando timestamps: DB={db_timestamp} vs TS={ts}")
            
            if db_timestamp <= ts:
                # Deleta do banco de dados
                cur.execute("DELETE FROM kv_store WHERE key = %s", (key_name,))
                pg_conn.commit()
                logger.info(f"✅ [del_key] Chave \"{key_name}\" removida do banco de dados em {timestamp}")
                
                # Também remove do Redis
                redis_client.delete(key_name)
                logger.info(f"✅ [del_key] Chave \"{key_name}\" removida do Redis")
            else:
                logger.info(f"⏩ [del_key] Remoção ignorada para \"{key_name}\" — valor mais recente existe.")
            
            # Confirma o processamento da mensagem
            ch.basic_ack(delivery_tag=method.delivery_tag)
            
    except Exception as e:
        logger.error(f"❌ [del_key] Erro: {str(e)}")
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

# Consumo das filas
rabbitmq_channel.basic_qos(prefetch_count=1)
rabbitmq_channel.basic_consume(queue='add_key', on_message_callback=handle_add_key)
rabbitmq_channel.basic_consume(queue='del_key', on_message_callback=handle_del_key)

logger.info("📬 Aguardando mensagens. Para sair pressione CTRL+C")
try:
    rabbitmq_channel.start_consuming()
except KeyboardInterrupt:
    logger.info("Consumidor interrompido pelo usuário")
    rabbitmq_channel.stop_consuming()
finally:
    if rabbitmq_conn:
        rabbitmq_conn.close()
    if pg_conn:
        pg_conn.close() 