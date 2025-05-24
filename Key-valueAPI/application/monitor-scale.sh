#!/usr/bin/env bash

# Prefixos dos serviços a monitorar
API_PREFIX="application-api"
CONSUMER_PREFIX="application-consumer"

# Percentagem limite de CPU para escalar
THRESHOLD=65

# Número máximo de réplicas
MAX_SCALE=7

# Intervalo de verificação em segundos
CHECK_INTERVAL=5

# Função que obtém a média de uso de CPU de um container
get_cpu_usage() {
  local container=$1
  # Pega a percentagem sem o sinal de % e converte para inteiro
  docker stats --no-stream --format "{{.Name}} {{.CPUPerc}}" "$container" \
    | awk '{ gsub(/%/,"",$2); print int($2) }'
}

# Loop infinito para monitoramento contínuo
while true; do
  echo "$(date '+%Y-%m-%d %H:%M:%S') - Verificando utilização de CPU dos serviços..."
  
  # Busca todos os containers API e consumer
  api_containers=$(docker ps --format "{{.Names}}" | grep "^${API_PREFIX}")
  consumer_containers=$(docker ps --format "{{.Names}}" | grep "^${CONSUMER_PREFIX}")
  
  # Verifica se encontrou containers
  if [ -z "$api_containers" ] && [ -z "$consumer_containers" ]; then
    echo "Aviso: nenhum container de API ou consumer encontrado."
    sleep "$CHECK_INTERVAL"
    continue
  fi
  
  # Verifica containers e decide se escala
  should_scale=false
  
  # Verifica containers de API
  for container in $api_containers; do
    cpu=$(get_cpu_usage "$container")
    echo "Uso de CPU de $container: ${cpu}%"
    if [ "$cpu" -gt "$THRESHOLD" ]; then
      should_scale=true
    fi
  done
  
  # Verifica containers de consumer
  for container in $consumer_containers; do
    cpu=$(get_cpu_usage "$container")
    echo "Uso de CPU de $container: ${cpu}%"
    if [ "$cpu" -gt "$THRESHOLD" ]; then
      should_scale=true
    fi
  done
  
  if ! $should_scale; then
    echo "Nenhum serviço acima de ${THRESHOLD}% de CPU. Não será efetuado scale."
    sleep "$CHECK_INTERVAL"
    continue
  fi
  
  # Calcula escala atual e próxima até MAX_SCALE
  current_scale=$(docker-compose ps --services --filter "status=running" | grep -c "^api")
  next_scale=$((current_scale + 1))
  if [ "$next_scale" -gt "$MAX_SCALE" ]; then
    next_scale=$MAX_SCALE
  fi
  
  if [ "$next_scale" -le "$current_scale" ]; then
    echo "Já atingido o número máximo de réplicas (${MAX_SCALE})."
    sleep "$CHECK_INTERVAL"
    continue
  fi
  
  echo "Escalando serviços para ${next_scale} réplicas..."
  docker-compose scale "api=${next_scale}" "consumer=${next_scale}"
  
  echo "Scale aplicado: api=${next_scale}, consumer=${next_scale}"
  
  # Aguarda intervalo antes de próxima verificação
  sleep "$CHECK_INTERVAL"
done
