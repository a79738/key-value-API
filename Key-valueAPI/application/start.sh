#!/usr/bin/env bash

# Cores para output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${GREEN}=== Iniciando Sistema ===${NC}"

# Verificar se o Docker está em execução
if ! docker info > /dev/null 2>&1; then
  echo -e "${RED}Docker não está em execução. Por favor, inicie o Docker e tente novamente.${NC}"
  exit 1
fi

# Criar diretórios necessários se não existirem
if [ ! -d "./cache" ]; then
  echo -e "${YELLOW}Criando diretório cache...${NC}"
  mkdir -p ./cache
fi

# Verificar se os arquivos de configuração do Redis Sentinel existem
for i in 1 2 3; do
  if [ ! -f "./cache/sentinel-$i.conf" ]; then
    echo -e "${YELLOW}Arquivo de configuração sentinel-$i.conf não encontrado. Criando...${NC}"
    cat > "./cache/sentinel-$i.conf" << EOF
port 26379
sentinel monitor mymaster 172.28.1.2 6379 2
sentinel down-after-milliseconds mymaster 5000
sentinel failover-timeout mymaster 60000
sentinel parallel-syncs mymaster 1
EOF
  fi
done

# Executar testes unitários da API
echo -e "${BLUE}Executando testes unitários da API...${NC}"
cd ./api && python -m pytest -v && cd ..

# Parar quaisquer containers antigos se existirem
echo -e "${YELLOW}Parando containers antigos se existirem...${NC}"
docker-compose down --remove-orphans

# Limpar volumes antigos se o parâmetro --clean for passado
if [ "$1" == "--clean" ]; then
  echo -e "${YELLOW}Limpando volumes antigos...${NC}"
  docker volume rm $(docker volume ls -q | grep -E 'application_crdb|application_redis') 2>/dev/null || true
fi

# Iniciar todos os serviços com um único comando
echo -e "${GREEN}Iniciando todos os serviços...${NC}"
if [ "$1" == "--detach" ] || [ "$2" == "--detach" ]; then
  docker-compose up --build -d
  
  # Verificar healthcheck das APIs
  echo -e "${BLUE}Aguardando serviços estarem saudáveis...${NC}"
  sleep 30
  for i in 1 2 3; do
    echo -e "${BLUE}Verificando API $i...${NC}"
    docker exec application-api$i curl -s http://localhost:3000/health | grep -q '"status":"ok"' && echo -e "${GREEN}API $i está saudável${NC}" || echo -e "${YELLOW}API $i ainda iniciando...${NC}"
  done
  
  echo -e "${GREEN}Serviços iniciados em segundo plano. Use 'docker-compose logs -f' para acompanhar os logs.${NC}"
  echo -e "${YELLOW}Para iniciar o monitor de escala, execute: './monitor-scale.sh'${NC}"
else
  docker-compose up --build 
fi 