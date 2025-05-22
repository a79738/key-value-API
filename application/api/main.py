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

def get_mq_channel():
    retries = 20
    delay = 5
    last_error = None
    
    print(f"🔄 Tentando conectar ao RabbitMQ (max {retries} tentativas)...")
    
    for attempt in range(1, retries + 1):
        try:
            print(f"🔄 Tentativa {attempt}/{retries} de conexão ao RabbitMQ")
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
            return channel
        except Exception as e:
            last_error = e
            print(f"⚠️ RabbitMQ não está pronto (tentativa {attempt}/{retries}): {str(e)}")
            if attempt == retries:
                print(f"❌ Falha ao conectar ao RabbitMQ após {retries} tentativas")
                raise Exception(f"❌ Não foi possível conectar ao RabbitMQ: {str(e)}")
            time.sleep(delay)

@asynccontextmanager
async def lifespan(app: FastAPI):
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
    
    # Aguardar um pouco para o RabbitMQ inicializar completamente
    print("⏳ Aguardando 5 segundos para o RabbitMQ estar disponível...")
    time.sleep(5)
    
    app.state.mq_channel = get_mq_channel()
    
    yield
    
app = FastAPI(lifespan=lifespan)

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