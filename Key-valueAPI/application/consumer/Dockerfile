FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1
# Configuração de prefetch para processar múltiplas mensagens simultaneamente
ENV PREFETCH_COUNT=20

CMD ["python", "consumer.py"]

