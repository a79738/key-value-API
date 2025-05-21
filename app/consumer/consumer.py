import pika
import redis
import psycopg2
import json
import os
import time
import logging
from datetime import datetime, timezone
from psycopg2 import OperationalError

# Configuração de logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Conexão Redis
def get_redis_client():
    max_retries = 30
    retry_count = 0
    while retry_count < max_retries:
        try:
            client = redis.Redis(
                host=os.getenv('REDIS_HOST', 'redis'),
                port=int(os.getenv('REDIS_PORT', 6379))
            )
            client.ping()  # Testa a conexão
            logger.info("✅ Conectado ao Redis com sucesso")
            return client
        except redis.RedisError as e:
            retry_count += 1
            logger.warning(f"Redis não está pronto. Tentativa {retry_count}/{max_retries}. Tentando novamente em 2 segundos... Erro: {str(e)}")
            time.sleep(2)
    raise Exception("Não foi possível conectar ao Redis após várias tentativas")

# Conexão PostgreSQL (CockroachDB)
def get_postgres_connection():
    max_retries = 30
    retry_count = 0
    while retry_count < max_retries:
        try:
            conn = psycopg2.connect(
                host='haproxy',
                port=26256,
                user='root',
                password='',
                database='appdb'
            )
            logger.info("✅ Conectado ao PostgreSQL (CockroachDB) com sucesso")
            
            # Verificar a conexão e exibir info do banco
            with conn.cursor() as cur:
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
            
            return conn
        except OperationalError as e:
            retry_count += 1
            logger.warning(f"PostgreSQL não está pronto. Tentativa {retry_count}/{max_retries}. Tentando novamente em 2 segundos... Erro: {str(e)}")
            time.sleep(2)
    raise Exception("Não foi possível conectar ao PostgreSQL após várias tentativas")

# Conexão RabbitMQ
def get_rabbitmq_connection(max_retries=10, delay=2):
    for attempt in range(1, max_retries + 1):
        try:
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
            logger.info("✅ Conectado ao RabbitMQ com sucesso")
            return connection, channel
        except pika.exceptions.AMQPConnectionError as e:
            logger.warning(f"RabbitMQ não está pronto. Tentativa {attempt}/{max_retries}. Tentando novamente em {delay} segundos... Erro: {str(e)}")
            if attempt == max_retries:
                raise Exception("Não foi possível conectar ao RabbitMQ após várias tentativas")
            time.sleep(delay)

def handle_add_key(ch, method, properties, body, pg_conn, redis_client):
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
        
        logger.info(f"📝 Tentando inserir/atualizar chave \"{key_name}\" com valor \"{key_value}\"")
        
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
            
            result = cur.fetchall()
            logger.info(f"📝 Resultado da operação no banco: {result}")
            
            if cur.rowcount == 0:
                logger.info(f"⏩ Atualização ignorada para \"{key_name}\" — valor mais recente já existe")
            else:
                logger.info(f"✅ [add_key] {key_name} definido como \"{key_value}\" em {timestamp}")
                
                # Também atualiza no Redis
                try:
                    redis_client.set(key_name, key_value)
                    logger.info(f"✅ [add_key] Chave {key_name} atualizada no Redis")
                except redis.RedisError as redis_err:
                    logger.error(f"⚠️ [add_key] Erro ao atualizar Redis: {str(redis_err)}")
        
        # Confirma o processamento da mensagem
        ch.basic_ack(delivery_tag=method.delivery_tag)
        
    except Exception as e:
        logger.error(f"❌ [add_key] Erro: {str(e)}", exc_info=True)
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

def handle_del_key(ch, method, properties, body, pg_conn, redis_client):
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
            
            # Verifica timestamp - note que db_timestamp já vem do PostgreSQL com timezone (offset-aware)
            db_timestamp = result[0]
            logger.info(f"Comparando timestamps: DB={db_timestamp} (tipo: {type(db_timestamp)}) vs TS={ts} (tipo: {type(ts)})")
            
            if db_timestamp <= ts:
                # Deleta do banco de dados
                cur.execute("DELETE FROM kv_store WHERE key = %s", (key_name,))
                pg_conn.commit()
                logger.info(f"✅ [del_key] Chave \"{key_name}\" removida do banco de dados em {timestamp}")
                
                # Também remove do Redis
                try:
                    redis_client.delete(key_name)
                    logger.info(f"✅ [del_key] Chave \"{key_name}\" removida do Redis")
                except redis.RedisError as redis_err:
                    logger.error(f"⚠️ [del_key] Erro ao remover do Redis: {str(redis_err)}")
            else:
                logger.info(f"⏩ [del_key] Remoção ignorada para \"{key_name}\" — valor mais recente existe.")
            
            # Confirma o processamento da mensagem
            ch.basic_ack(delivery_tag=method.delivery_tag)
            
    except Exception as e:
        logger.error(f"❌ [del_key] Erro: {str(e)}", exc_info=True)
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

def main():
    try:
        # Conecta a todos os serviços
        redis_client = get_redis_client()
        pg_conn = get_postgres_connection()
        rabbitmq_conn, rabbitmq_channel = get_rabbitmq_connection()
        
        # Configura os consumidores
        rabbitmq_channel.basic_qos(prefetch_count=1)
        
        # Callback para add_key
        def on_add_key_message(ch, method, properties, body):
            handle_add_key(ch, method, properties, body, pg_conn, redis_client)
        
        # Callback para del_key
        def on_del_key_message(ch, method, properties, body):
            handle_del_key(ch, method, properties, body, pg_conn, redis_client)
        
        # Registra os consumidores
        rabbitmq_channel.basic_consume(queue='add_key', on_message_callback=on_add_key_message)
        rabbitmq_channel.basic_consume(queue='del_key', on_message_callback=on_del_key_message)
        
        logger.info("📬 Aguardando mensagens. Para sair pressione CTRL+C")
        rabbitmq_channel.start_consuming()
        
    except KeyboardInterrupt:
        logger.info("Consumidor interrompido pelo usuário")
        if 'rabbitmq_channel' in locals() and rabbitmq_channel is not None:
            rabbitmq_channel.stop_consuming()
        
    except Exception as e:
        logger.error(f"Erro fatal: {str(e)}", exc_info=True)
        
    finally:
        # Fecha conexões
        if 'rabbitmq_conn' in locals() and rabbitmq_conn is not None:
            try:
                rabbitmq_conn.close()
                logger.info("Conexão RabbitMQ fechada")
            except:
                pass
                
        if 'pg_conn' in locals() and pg_conn is not None:
            try:
                pg_conn.close()
                logger.info("Conexão PostgreSQL fechada")
            except:
                pass
        
        logger.info("Consumidor finalizado")

if __name__ == "__main__":
    main() 