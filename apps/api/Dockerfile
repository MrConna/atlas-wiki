FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY app ./app
COPY alembic.ini ./
COPY migrations ./migrations
COPY docker-entrypoint.sh ./
RUN pip install --no-cache-dir .
EXPOSE 8000
CMD ["sh", "./docker-entrypoint.sh"]
