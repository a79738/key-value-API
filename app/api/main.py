from fastapi import FastAPI, HTTPException, Query, Depends
from pydantic import BaseModel, Field
import redis
from redis.exceptions import RedisError
import psycopg2
from psycopg2 import OperationalError
import pika
import json
import os
import time
import logging
from typing import Optional

# Configuração de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# Set para rastrear chaves pendentes de deleção
pending_deletions = set()

# Conexões com serviços externos
redis_client = None
pg_conn = None
rabbitmq_channel = None

# Modelos de dados
class KeyValue(BaseModel):
    key_name: str = Field(..., min_length=1, max_length=100)
    key_value: str = Field(..., min_length=1, max_length=1000)

class KeyName(BaseModel):
    key_name: str = Field(..., min_length=1, max_length=100)

# Inicialização das conexões
@app.on_event("startup")
async def startup_event():
    global redis_client, pg_conn, rabbitmq_channel
    
    # Conectar ao Redis
    redis_client = redis.Redis(
        host=os.getenv('REDIS_HOST', 'redis'),
        port=int(os.getenv('REDIS_PORT', 6379))
    )
    logger.info("✅ Conectado ao Redis")
    
    # Conectar ao PostgreSQL
    pg_conn = psycopg2.connect(
        host=os.getenv('POSTGRES_HOST', 'haproxy'),
        port=int(os.getenv('POSTGRES_PORT', 26256)),
        user=os.getenv('POSTGRES_USER', 'root'),
        password=os.getenv('POSTGRES_PASSWORD', ''),
        database=os.getenv('POSTGRES_DB', 'appdb')
    )
    logger.info("✅ Conectado ao PostgreSQL")
    
    # Conectar ao RabbitMQ
    connection = pika.BlockingConnection(
        pika.ConnectionParameters(host=os.getenv('RABBITMQ_HOST', 'rabbitmq'))
    )
    rabbitmq_channel = connection.channel()
    rabbitmq_channel.queue_declare(queue='add_key', durable=True)
    rabbitmq_channel.queue_declare(queue='del_key', durable=True)
    logger.info("✅ Conectado ao RabbitMQ")

@app.on_event("shutdown")
async def shutdown_event():
    if pg_conn:
        pg_conn.close()
    if rabbitmq_channel:
        rabbitmq_channel.close()

# Endpoints
@app.get("/")
async def get_value(key: str = Query(..., min_length=1, max_length=100)):
    global redis_client, pg_conn, pending_deletions
    
    try:
        # Verifica se a chave está pendente de exclusão
        if key in pending_deletions:
            raise HTTPException(status_code=404, detail="Key is pending deletion")
            
        # Verifica se as conexões estão estabelecidas
        if redis_client is None or pg_conn is None:
            logger.error("Conexões não estabelecidas")
            raise HTTPException(status_code=500, detail="Database connections not established")
            
        # Tenta buscar do Redis primeiro
        try:
            redis_value = redis_client.get(key)
            if redis_value is not None:
                return {"value": redis_value.decode(), "source": "redis"}
        except RedisError as re:
            logger.error(f"Erro no Redis: {str(re)}")
            # Continua para tentar no banco de dados
        
        # Se não encontrar no Redis, busca no banco de dados
        try:
            with pg_conn.cursor() as cur:
                cur.execute("SELECT value FROM kv_store WHERE key = %s", (key,))
                result = cur.fetchone()
                
                if result:
                    value = result[0]
                    # Armazena no Redis para futuras consultas
                    try:
                        redis_client.set(key, value)
                    except RedisError as re:
                        logger.warning(f"Não foi possível armazenar em cache no Redis: {str(re)}")
                    
                    return {"value": value, "source": "database"}
                
                raise HTTPException(status_code=404, detail="Key not found")
        except psycopg2.Error as dbe:
            logger.error(f"Erro no banco de dados: {str(dbe)}")
            raise HTTPException(status_code=500, detail="Database error")
            
    except Exception as e:
        logger.error(f"Erro não tratado: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal Server Error")

@app.put("/")
async def put_value(key_value: KeyValue):
    global pending_deletions
    
    try:
        # Remove da lista de exclusões pendentes se estiver lá
        if key_value.key_name in pending_deletions:
            pending_deletions.remove(key_value.key_name)
        
        payload = {
            "key_name": key_value.key_name,
            "key_value": key_value.key_value,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        }
        
        rabbitmq_channel.basic_publish(
            exchange='',
            routing_key='add_key',
            body=json.dumps(payload),
            properties=pika.BasicProperties(
                delivery_mode=2  # torna a mensagem persistente
            )
        )
        
        return {"message": f"Key '{key_value.key_name}' queued to add"}
    except Exception as e:
        logger.error(f"Erro na adição: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal Server Error")

@app.delete("/")
async def delete_value(key_name: str = Query(..., min_length=1, max_length=100)):
    global redis_client, pending_deletions
    
    try:
        # Adiciona ao conjunto de exclusões pendentes
        pending_deletions.add(key_name)
        
        # Remove do Redis
        if redis_client:
            try:
                redis_client.delete(key_name)
                logger.info(f"Chave '{key_name}' removida do Redis")
            except RedisError as re:
                logger.error(f"Erro ao remover do Redis: {str(re)}")
        
        payload = {
            "key_name": key_name,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        }
        
        rabbitmq_channel.basic_publish(
            exchange='',
            routing_key='del_key',
            body=json.dumps(payload),
            properties=pika.BasicProperties(
                delivery_mode=2  # torna a mensagem persistente
            )
        )
        
        return {"message": f"Key '{key_name}' queued for deletion"}
    except Exception as e:
        logger.error(f"Erro na exclusão: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal Server Error")

@app.get("/health")
async def health_check():
    return {"status": "healthy"} 