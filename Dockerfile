FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=Asia/Shanghai
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY quant_assistant ./quant_assistant
RUN mkdir -p data/reports data/cache && useradd --uid 10001 runner && chown -R runner /app
USER runner
ENTRYPOINT ["python", "-m", "quant_assistant.production_job"]
