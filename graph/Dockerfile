ARG APP_IMAGE=auto-reviewer:local
FROM ${APP_IMAGE}
USER root
COPY graph/requirements.lock /opt/graph-requirements.lock
RUN python -m venv --system-site-packages /opt/graph-venv && /opt/graph-venv/bin/pip install --no-cache-dir -r /opt/graph-requirements.lock
COPY graph/entry.py /opt/graph-entry.py
ENV PYTHONPATH=/app CRG_HOME=/tmp/crg CRG_PARSE_WORKERS=2
USER 10001:10001
ENTRYPOINT ["/opt/graph-venv/bin/python", "/opt/graph-entry.py"]
