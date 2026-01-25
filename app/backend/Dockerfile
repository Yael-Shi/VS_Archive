FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN pip install --no-cache-dir poetry

COPY pyproject.toml poetry.lock* /app/
RUN poetry config virtualenvs.create false \
 && poetry install --no-interaction --no-ansi --no-root

COPY . /app

EXPOSE 8000

CMD ["bash", "-lc", "python manage.py migrate && python manage.py runserver 0.0.0.0:8000"]
