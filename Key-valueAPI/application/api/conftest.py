import pytest
from unittest.mock import MagicMock

@pytest.fixture(scope="session", autouse=True)
def mock_environment(monkeypatch):
    """
    Configura o ambiente de teste substituindo as variáveis de ambiente
    para evitar dependências externas durante os testes
    """
    env_vars = {
        "REDIS_USE_SENTINEL": "false",
        "REDIS_HOST": "localhost",
        "REDIS_PORT": "6379",
        "POSTGRES_HOST": "localhost",
        "POSTGRES_PORT": "5432",
        "POSTGRES_USER": "test_user",
        "POSTGRES_PASSWORD": "test_password",
        "POSTGRES_DB": "test_db",
        "RABBIT_URL": "amqp://guest:guest@localhost:5672/"
    }
    
    for key, value in env_vars.items():
        monkeypatch.setenv(key, value)
    
    # Mock para função lifespan da FastAPI para evitar tentativas de conexão
    # com serviços externos durante inicialização da aplicação
    async def mock_lifespan_fn(app):
        app.state.mq_channel = MagicMock()
        yield
    
    monkeypatch.setattr("main.lifespan", mock_lifespan_fn) 