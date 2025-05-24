FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Comando para rodar em produção
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "3000"]

# Comando para rodar os testes (usar como override em dev)
# docker run --rm -it [image] pytest

