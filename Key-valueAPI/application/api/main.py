from fastapi import FastAPI, HTTPException, Query, Body, Depends
from pydantic import BaseModel, Field, validator
from typing import Optional
import redis
import psycopg2
import pika
import json
import os
from datetime import datetime
import time
import logging
import pytz
from contextlib import asynccontextmanager

class KeyPayload(BaseModel):
    key_name: str = Field(..., min_length=1, max_length=100)
    key_value: str = Field(..., min_length=1, max_length=1000)

class KeyDelete(BaseModel):
    key_name: str = Field(..., min_length=1, max_length=100)

class HealthResponse(BaseModel):
    status: str
    redis: str
    database: str
    rabbitmq: str
    version: str = "1.0.0"
    timestamp: str

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

def get_db_connection():
    return psycopg2.connect(
        host=os.environ.get('POSTGRES_HOST', 'haproxy'),
        port=int(os.environ.get('POSTGRES_PORT', 26256)),
        user=os.environ.get('POSTGRES_USER', 'root'),
        password=os.environ.get('POSTGRES_PASSWORD', ''),
        database=os.environ.get('POSTGRES_DB', 'appdb')
    )

def get_mq_channel():
    retries = 20
    delay = 5
    last_error = None
    
    # Usar URL completa para conexão RabbitMQ
    rabbit_url = os.environ.get('RABBIT_URL', 'amqp://appuser:s3nh4segura@broker-1:5672/')
    
    print(f"Connecting to RabbitMQ: {rabbit_url} (max {retries} attempts)...")
    
    for attempt in range(1, retries + 1):
        try:
            print(f"Attempt {attempt}/{retries} to connect to RabbitMQ")
            connection_params = pika.URLParameters(rabbit_url)
            connection_params.heartbeat = 600
            connection_params.blocked_connection_timeout = 300
            connection = pika.BlockingConnection(connection_params)
            
            channel = connection.channel()
            channel.queue_declare(queue='add_key', durable=True)
            channel.queue_declare(queue='del_key', durable=True)
            print("Connected to RabbitMQ")
            return channel
        except Exception as e:
            last_error = e
            print(f"RabbitMQ not ready (attempt {attempt}/{retries}): {str(e)}")
            if attempt == retries:
                print(f"Failed to connect to RabbitMQ after {retries} attempts")
                # Tentativa final: conectar via localhost
                try:
                    print(f"Trying localhost connection...")
                    connection = pika.BlockingConnection(
                        pika.URLParameters('amqp://appuser:s3nh4segura@localhost:5672/')
                    )
                    channel = connection.channel()
                    channel.queue_declare(queue='add_key', durable=True)
                    channel.queue_declare(queue='del_key', durable=True)
                    print("Connected to RabbitMQ via localhost")
                    return channel
                except Exception as localhost_err:
                    print(f"Localhost connection failed: {str(localhost_err)}")
                
                raise Exception(f"Cannot connect to RabbitMQ: {str(e)}")
            time.sleep(delay)

@asynccontextmanager
async def lifespan(app: FastAPI):
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
    
    # Aguardar um pouco para o RabbitMQ inicializar completamente
    print("Waiting 20 seconds for RabbitMQ...")
    time.sleep(20)
    
    try:
        app.state.mq_channel = get_mq_channel()
    except Exception as e:
        print(f"Cannot connect to RabbitMQ: {str(e)}")
        # Inicializa mesmo assim, API vai dar erro em endpoints que usam RabbitMQ
        app.state.mq_channel = None
    
    yield
    
app = FastAPI(lifespan=lifespan)

@app.get("/health")
async def health_check():
    """
    Endpoint de health check que verifica a conexão com todos os serviços dependentes
    """
    now = datetime.now(pytz.UTC)
    health = {
        "status": "ok",
        "redis": "ok",
        "database": "ok",
        "rabbitmq": "ok" if app.state.mq_channel is not None else "error",
        "timestamp": now.isoformat()
    }
    
    # Verificar Redis
    try:
        redis_client.ping()
    except Exception:
        health["redis"] = "error"
        health["status"] = "degraded"
    
    # Verificar Banco de Dados
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT 1')
        cursor.close()
        conn.close()
    except Exception:
        health["database"] = "error"
        health["status"] = "degraded"
    
    # Se nenhum serviço estiver disponível, considerar sistema como down
    if health["redis"] == "error" and health["database"] == "error" and health["rabbitmq"] == "error":
        health["status"] = "down"
    
    return health

@app.get("/")
async def get_key(key: str = Query(..., min_length=1, max_length=100)):
    try:
        value = redis_client.get(key)
        if value is not None:
            return {"value": value, "source": "redis"}
        
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT value FROM kv_store WHERE key = %s', (key,))
        result = cursor.fetchone()
        cursor.close()
        conn.close()
        
        if result:
            value = result[0]
            redis_client.set(key, value)
            return {"value": value, "source": "database"}
        
        raise HTTPException(status_code=404, detail="Key not found")
    except Exception as e:
        print(f"Error: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal Server Error")

@app.put("/")
async def update_key(payload: KeyPayload):
    if app.state.mq_channel is None:
        raise HTTPException(status_code=503, detail="RabbitMQ connection not available")
        
    # Usar datetime com timezone para evitar erro de comparação
    now = datetime.now(pytz.UTC)
    data = {
        "key_name": payload.key_name,
        "key_value": payload.key_value,
        "timestamp": now.isoformat()
    }
    
    app.state.mq_channel.basic_publish(
        exchange='',
        routing_key='add_key',
        body=json.dumps(data),
        properties=pika.BasicProperties(
            delivery_mode=2,  # make message persistent
        )
    )
    
    return {"message": "Queued to add_key", "status_code": 202}

@app.delete("/")
async def delete_key(key_name: str = Query(..., min_length=1, max_length=100)):
    if app.state.mq_channel is None:
        raise HTTPException(status_code=503, detail="RabbitMQ connection not available")
        
    # Usar datetime com timezone para evitar erro de comparação
    now = datetime.now(pytz.UTC)
    data = {
        "key_name": key_name,
        "timestamp": now.isoformat()
    }
    
    app.state.mq_channel.basic_publish(
        exchange='',
        routing_key='del_key',
        body=json.dumps(data),
        properties=pika.BasicProperties(
            delivery_mode=2,  # make message persistent
        )
    )
    
    return {"message": "Queued to del_key", "status_code": 202}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=3000) 