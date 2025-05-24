import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock, patch
import json
import redis
import pika
import psycopg2
from datetime import datetime

# Mock dos módulos externos para evitar dependências reais na execução do teste
@pytest.fixture(autouse=True)
def mock_dependencies(monkeypatch):
    # Mock Redis
    mock_redis = MagicMock()
    mock_redis.get.return_value = None
    monkeypatch.setattr("redis.Redis", lambda **kwargs: mock_redis)
    monkeypatch.setattr("redis.sentinel.Sentinel", lambda *args, **kwargs: MagicMock())
    
    # Mock PG connection
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = ["mock_value"]
    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    monkeypatch.setattr("psycopg2.connect", lambda **kwargs: mock_conn)
    
    # Mock RabbitMQ
    mock_channel = MagicMock()
    mock_connection = MagicMock()
    mock_connection.channel.return_value = mock_channel
    monkeypatch.setattr("pika.BlockingConnection", lambda *args, **kwargs: mock_connection)

# Importando a aplicação FastAPI após o mock das dependências
from main import app

@pytest.fixture
def client():
    return TestClient(app)

def test_get_key_from_db(client, monkeypatch):
    # Este teste verifica se a rota GET consegue buscar uma chave do banco de dados
    # quando não está no Redis
    
    # Mock Redis para não encontrar a chave
    mock_redis_client = MagicMock()
    mock_redis_client.get.return_value = None
    monkeypatch.setattr("main.redis_client", mock_redis_client)
    
    # Mock conexão com o banco de dados
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = ["teste_valor"]
    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    
    with patch("main.get_db_connection", return_value=mock_conn):
        response = client.get("/?key=teste_key")
        
    assert response.status_code == 200
    assert response.json() == {"value": "teste_valor", "source": "database"}

def test_get_key_from_redis(client, monkeypatch):
    # Este teste verifica se a rota GET consegue buscar uma chave diretamente do Redis
    
    # Mock Redis para encontrar a chave
    mock_redis_client = MagicMock()
    mock_redis_client.get.return_value = "valor_cache"
    monkeypatch.setattr("main.redis_client", mock_redis_client)
    
    response = client.get("/?key=teste_key")
    
    assert response.status_code == 200
    assert response.json() == {"value": "valor_cache", "source": "redis"}

def test_get_key_not_found(client, monkeypatch):
    # Este teste verifica se a API retorna 404 quando a chave não é encontrada
    
    # Mock Redis para não encontrar a chave
    mock_redis_client = MagicMock()
    mock_redis_client.get.return_value = None
    monkeypatch.setattr("main.redis_client", mock_redis_client)
    
    # Mock banco de dados para não encontrar a chave
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = None
    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    
    with patch("main.get_db_connection", return_value=mock_conn):
        response = client.get("/?key=chave_inexistente")
        
    assert response.status_code == 404
    assert "Key not found" in response.json().get("detail")

def test_add_key(client, monkeypatch):
    # Este teste verifica se a rota PUT funciona corretamente
    
    # Mock do canal RabbitMQ
    mock_channel = MagicMock()
    monkeypatch.setattr("main.app.state.mq_channel", mock_channel)
    
    response = client.put(
        "/",
        json={"key_name": "teste_key", "key_value": "teste_valor"}
    )
    
    assert response.status_code == 200
    assert response.json().get("message") == "Queued to add_key"
    assert mock_channel.basic_publish.called

def test_delete_key(client, monkeypatch):
    # Este teste verifica se a rota DELETE funciona corretamente
    
    # Mock do canal RabbitMQ
    mock_channel = MagicMock()
    monkeypatch.setattr("main.app.state.mq_channel", mock_channel)
    
    response = client.delete("/?key_name=teste_key")
    
    assert response.status_code == 200
    assert response.json().get("message") == "Queued to del_key"
    assert mock_channel.basic_publish.called 