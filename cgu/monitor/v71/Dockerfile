# Dependencies only. prom_v2.py stays in the node-resource-monitor-script-v71
# ConfigMap (mounted at /opt/monitor), so a code change needs no rebuild.
# The base is pinned by digest: the plain python:3.11-slim tag moves.
FROM python:3.11-slim@sha256:ae52c5bef62a6bdd42cd1e8dffef86b9cd284bde9427da79839de7a4b983e7ca
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && pip freeze > /requirements.lock && rm /tmp/requirements.txt
ENV PYTHONUNBUFFERED=1
CMD ["python", "/opt/monitor/prom_v2.py"]
