FROM python:3.12-slim
ARG APP_GIT_SHA=unknown
ENV APP_GIT_SHA=${APP_GIT_SHA}
WORKDIR /app
COPY pyproject.toml ./
COPY app ./app
COPY alembic.ini ./
COPY migrations ./migrations
COPY docker-entrypoint.sh ./
RUN pip install --no-cache-dir .
EXPOSE 8000
CMD ["sh", "./docker-entrypoint.sh"]
