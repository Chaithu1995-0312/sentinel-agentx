FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
ENV PORT=8004
EXPOSE 8004
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8004}"]
