FROM python:3.12-slim

WORKDIR /app

# System deps for psycopg2, pyodbc etc.
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create runtime directories
RUN mkdir -p audits sessions uploads

EXPOSE 7788

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "7788", "--workers", "1"]
