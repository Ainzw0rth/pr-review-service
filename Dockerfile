FROM docker:29-cli@sha256:b1805116a6a86cc591b5d5f60a910a0715cdcc9d18d866ad68b1457ead25c35c AS dockercli
FROM python:3.12-slim@sha256:ddb0207ae1f0356c2b724d740769b0c5f5f51cc54a0525178f721825f78fe74c
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock .
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client-17 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -r requirements.lock && useradd --uid 10001 --create-home app
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
COPY . .
RUN mkdir -p /app/var /app/staticfiles && chown -R app:app /app/var /app/staticfiles && DJANGO_SECRET_KEY=build-only-static-key python manage.py collectstatic --noinput
USER 10001:10001
EXPOSE 8040
CMD ["gunicorn", "reviewdesk.wsgi:application", "--bind=0.0.0.0:8040", "--workers=2", "--threads=2", "--timeout=60", "--access-logfile=/dev/null", "--no-control-socket"]
