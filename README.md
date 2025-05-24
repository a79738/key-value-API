# Sistema Distribuído de Chave-Valor

Este sistema implementa um serviço avançado de armazenamento distribuído de chave-valor projetado para alta disponibilidade, escalabilidade e baixa latência. A arquitetura adota o princípio de responsabilidade única, separando operações de leitura (síncronas) e escrita (assíncronas) através de filas de mensagens, permitindo melhor desempenho em cenários de alta carga.

O sistema utiliza estratégias de cache inteligente para otimizar o tempo de resposta e implementa técnicas de consistência eventual para melhorar a disponibilidade dos dados. A redundância em todos os componentes garante a continuidade do serviço mesmo na presença de falhas parciais.

## Arquitetura

O sistema é composto por:

- **API REST**: Múltiplas instâncias FastAPI que processam requisições HTTP
- **Cache**: Redis com replicação e Sentinel para alta disponibilidade
- **Banco de dados**: Cluster CockroachDB acessado via HAProxy para persistência
- **Fila de mensagens**: Cluster RabbitMQ para processamento assíncrono
- **Consumidores**: Processadores de mensagens que atualizam o banco de dados e invalidam o cache
- **Load balancers**: Nginx (para APIs) e HAProxy (para banco de dados)

## Fluxo de Dados

- **Leitura**: Primeiro verifica-se no Redis, se não encontrado, busca no CockroachDB
- **Escrita/Exclusão**: Envia mensagem para RabbitMQ que é processada assincronamente por consumidores

## Otimizações

- Prefetch count nos consumidores para processamento paralelo de mensagens
- Invalidação inteligente de cache para garantir consistência eventual
- Sistema de reconexão automática para melhor resiliência
- Health checks para detecção proativa de falhas
- Testes automatizados integrados ao processo de inicialização

## Implementação em Servidor ou Cloud

O sistema foi projetado seguindo princípios cloud-native e pode ser facilmente implantado em ambientes de produção. Para migrar do ambiente de desenvolvimento para produção em um servidor ou nuvem, recomenda-se utilizar Kubernetes como orquestrador de contêineres.

A migração envolve converter o docker-compose.yml em manifestos Kubernetes (Deployments, Services, StatefulSets), configurar volumes persistentes para Redis e CockroachDB, e implementar Ingress Controllers para gerenciamento de tráfego externo. Para monitoramento, a stack Prometheus/Grafana pode ser integrada através dos endpoints de health check já disponíveis na API.

Em ambientes de nuvem pública (AWS, GCP, Azure), pode-se optar por serviços gerenciados como ElastiCache (AWS), Cloud Memorystore (GCP) ou Azure Cache para Redis, reduzindo o overhead operacional.

## Requisitos

- Docker
- Docker Compose

## Executando o sistema

Para iniciar todos os serviços:

```bash
./start.sh
```

## Testando o serviço

### Usando a API

```bash
# Criando ou atualizando uma chave
curl -X PUT http://localhost:8000/ \
  -H "Content-Type: application/json" \
  -d '{"key_name":"teste", "key_value":"valor"}'

# Consultando uma chave
curl -X GET http://localhost:8000/?key=teste

# Removendo uma chave
curl -X DELETE http://localhost:8000/?key_name=teste
```

## Monitoramento e Escalabilidade

O sistema inclui um script de monitoramento que escala automaticamente os serviços com base na utilização de CPU:

```bash
./monitor-scale.sh
``` 

## Relatório de Testes ao Sistema
Neste relatório segue os testes feitos para testar e justificar os limites deste sistema. o relatório em qustão segue no diretorio de documentação.


