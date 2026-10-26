FROM docker:29-cli@sha256:b1805116a6a86cc591b5d5f60a910a0715cdcc9d18d866ad68b1457ead25c35c AS dockercli
FROM python:3.12-slim@sha256:ddb0207ae1f0356c2b724d740769b0c5f5f51cc54a0525178f721825f78fe74c
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
COPY execution/ /app/execution/
# Trusted local Docker orchestrator; disposable execution containers have separate restrictions.
# No application requirements, Django, database credentials or coordinator .env are copied.
ENTRYPOINT ["python", "-m", "execution.agent"]
