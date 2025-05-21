from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
import redis
import psycopg2
import pika
import json
import os
import time
import logging
from psycopg2 import OperationalError
from redis.exceptions import RedisError

# Configuração de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

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
        except RedisError:
            retry_count += 1
            logger.warning(f"Redis não está pronto. Tentativa {retry_count}/{max_retries}. Tentando novamente em 2 segundos...")
            time.sleep(2)
    raise Exception("Não foi possível conectar ao Redis após várias tentativas")

def get_postgres_connection():
    max_retries = 30
    retry_count = 0
    while retry_count < max_retries:
        try:
            conn = psycopg2.connect(
                host=os.getenv('POSTGRES_HOST', 'haproxy'),
                port=int(os.getenv('POSTGRES_PORT', 26256)),
                user=os.getenv('POSTGRES_USER', 'root'),
                password=os.getenv('POSTGRES_PASSWORD', ''),
                database=os.getenv('POSTGRES_DB', 'appdb')
            )
            logger.info("✅ Conectado ao PostgreSQL com sucesso")
            return conn
        except OperationalError:
            retry_count += 1
            logger.warning(f"PostgreSQL não está pronto. Tentativa {retry_count}/{max_retries}. Tentando novamente em 2 segundos...")
            time.sleep(2)
    raise Exception("Não foi possível conectar ao PostgreSQL após várias tentativas")

# Configuração do RabbitMQ
def get_rabbitmq_channel():
    max_retries = 30
    retry_count = 0
    while retry_count < max_retries:
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
            return channel
        except pika.exceptions.AMQPConnectionError:
            retry_count += 1
            logger.warning(f"RabbitMQ não está pronto. Tentativa {retry_count}/{max_retries}. Tentando novamente em 2 segundos...")
            time.sleep(2)
    
    raise Exception("Não foi possível conectar ao RabbitMQ após várias tentativas")

# Inicialização das conexões
redis_client = None
pg_conn = None
rabbitmq_channel = None

@app.on_event("startup")
async def startup_event():
    global redis_client, pg_conn, rabbitmq_channel
    try:
        logger.info("Iniciando conexões com serviços externos...")
        redis_client = get_redis_client()
        pg_conn = get_postgres_connection()
        rabbitmq_channel = get_rabbitmq_channel()
        logger.info("Todas as conexões inicializadas com sucesso")
    except Exception as e:
        logger.error(f"Erro fatal durante a inicialização: {str(e)}", exc_info=True)
        raise

@app.on_event("shutdown")
async def shutdown_event():
    logger.info("Finalizando conexões...")
    try:
        if pg_conn:
            pg_conn.close()
            logger.info("Conexão com PostgreSQL fechada")
    except Exception as e:
        logger.error(f"Erro ao fechar conexão com PostgreSQL: {str(e)}")
    
    try:
        if rabbitmq_channel and rabbitmq_channel.is_open:
            rabbitmq_channel.close()
            logger.info("Canal RabbitMQ fechado")
    except Exception as e:
        logger.error(f"Erro ao fechar canal RabbitMQ: {str(e)}")
    
    logger.info("Aplicação finalizada")

# Modelos Pydantic
class KeyValue(BaseModel):
    key_name: str = Field(..., min_length=1, max_length=100)
    key_value: str = Field(..., min_length=1, max_length=1000)

class KeyName(BaseModel):
    key_name: str = Field(..., min_length=1, max_length=100)

@app.get("/")
async def get_value(key_name: str = Query(..., min_length=1, max_length=100)):
    global redis_client, pg_conn
    try:
        logger.info(f"Buscando valor para chave: {key_name}")
        
        # Verifica se o cliente Redis está disponível
        if redis_client is None:
            logger.error("Cliente Redis não inicializado!")
            redis_client = get_redis_client()
        
        # Tenta buscar do Redis primeiro
        try:
            redis_value = redis_client.get(key_name)
            if redis_value is not None:
                logger.info(f"Valor para chave '{key_name}' encontrado no Redis")
                return {"key": key_name, "value": redis_value.decode(), "source": "redis"}
        except RedisError as redis_err:
            logger.error(f"Erro ao buscar do Redis: {str(redis_err)}")
            # Continua para tentar buscar do banco de dados
        
        # Verifica a conexão com o banco de dados
        if pg_conn is None or pg_conn.closed:
            logger.warning("Conexão com o banco de dados não disponível. Reconectando...")
            pg_conn = get_postgres_connection()
        
        # Se não encontrar no Redis, busca no banco de dados
        try:
            with pg_conn.cursor() as cur:
                cur.execute("SELECT value FROM kv_store WHERE key = %s", (key_name,))
                result = cur.fetchone()
                
                if result:
                    value = result[0]
                    logger.info(f"Valor para chave '{key_name}' encontrado no banco de dados")
                    
                    # Armazena no Redis para futuras consultas
                    try:
                        redis_client.set(key_name, value)
                        logger.info(f"Valor para chave '{key_name}' armazenado no Redis")
                    except RedisError as re:
                        logger.error(f"Erro ao armazenar no Redis: {str(re)}")
                    
                    return {"key": key_name, "value": value, "source": "database"}
                
                logger.warning(f"Chave '{key_name}' não encontrada em nenhum armazenamento")
                raise HTTPException(status_code=404, detail="Chave não encontrada")
        except OperationalError as oe:
            logger.error(f"Erro de operação no banco de dados: {str(oe)}")
            raise HTTPException(status_code=500, detail="Erro ao consultar banco de dados")
                
    except Exception as e:
        logger.error(f"Erro inesperado durante busca: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Erro ao processar solicitação de busca")

@app.get("/health")
async def health_check():
    return {"status": "healthy"}

@app.put("/")
async def put_value(key_value: KeyValue):
    try:
        logger.info(f"Processando PUT para chave: {key_value.key_name}")
        
        payload = {
            "key_name": key_value.key_name,
            "key_value": key_value.key_value,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        }
        
        # Verifica se o rabbitmq_channel está disponível
        global rabbitmq_channel
        if rabbitmq_channel is None or not rabbitmq_channel.is_open:
            logger.warning("Canal RabbitMQ não disponível. Reconectando...")
            rabbitmq_channel = get_rabbitmq_channel()
        
        try:
            logger.info(f"Enviando chave {key_value.key_name} para fila de adição")
            rabbitmq_channel.basic_publish(
                exchange='',
                routing_key='add_key',
                body=json.dumps(payload),
                properties=pika.BasicProperties(
                    delivery_mode=2  # torna a mensagem persistente
                )
            )
            logger.info(f"Mensagem para chave {key_value.key_name} enviada com sucesso para RabbitMQ")
            
            # Cache imediato no Redis para resposta mais rápida
            try:
                if redis_client is not None:
                    redis_client.set(key_value.key_name, key_value.key_value)
                    logger.info(f"Valor para {key_value.key_name} armazenado em cache Redis")
            except RedisError as re:
                logger.error(f"Erro ao armazenar em cache: {str(re)}")
                # Não falha a operação principal se o cache falhar
            
            return {"message": f"Chave '{key_value.key_name}' enfileirada para processamento", "success": True}
        except pika.exceptions.AMQPError as mq_err:
            logger.error(f"Erro ao enviar para RabbitMQ: {str(mq_err)}")
            raise HTTPException(status_code=500, detail=f"Erro ao enfileirar para processamento: {str(mq_err)}")
    except Exception as e:
        logger.error(f"Erro inesperado durante PUT: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Erro ao processar solicitação PUT")

@app.delete("/")
async def delete_value(key_name: str = Query(..., min_length=1, max_length=100)):
    global redis_client, pg_conn, rabbitmq_channel
    try:
        logger.info(f"Iniciando deleção da chave: {key_name}")
        
        # Verifica se o cliente Redis está disponível
        if redis_client is None:
            logger.error("Cliente Redis não inicializado!")
            redis_client = get_redis_client()
        
        # Tenta remover do Redis primeiro - sem verificar existência
        try:
            logger.info(f"Removendo chave {key_name} do Redis")
            redis_client.delete(key_name)
            logger.info(f"Chave {key_name} removida do Redis com sucesso")
        except RedisError as redis_err:
            logger.error(f"Erro ao remover do Redis: {str(redis_err)}")
            # Continua mesmo com erro no Redis
        
        # Verifica a conexão com o banco de dados
        if pg_conn is None or pg_conn.closed:
            logger.warning("Conexão com o banco de dados não disponível. Reconectando...")
            pg_conn = get_postgres_connection()
        
        # Procede com o envio para o RabbitMQ sem verificar existência
        payload = {
            "key_name": key_name,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        }
        
        try:
            logger.info(f"Enviando chave {key_name} para fila de deleção")
            rabbitmq_channel.basic_publish(
                exchange='',
                routing_key='del_key',
                body=json.dumps(payload),
                properties=pika.BasicProperties(
                    delivery_mode=2  # torna a mensagem persistente
                )
            )
            logger.info("Mensagem enviada com sucesso para RabbitMQ")
        except pika.exceptions.AMQPError as mq_err:
            logger.error(f"Erro ao enviar para RabbitMQ: {str(mq_err)}")
            raise HTTPException(status_code=500, detail=f"Erro ao enfileirar deleção: {str(mq_err)}")
        
        return {"message": f"Chave '{key_name}' removida do sistema", "success": True}
    except Exception as e:
        logger.error(f"Erro inesperado durante deleção: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Erro ao processar solicitação de deleção") 