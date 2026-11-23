FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Europe/Warsaw TE_ROOT=/app
WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir -e '.[dev]'

COPY sources.yaml ./
COPY tests ./tests

ENTRYPOINT ["tarnow-events"]
CMD ["run"]
