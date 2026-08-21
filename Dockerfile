# Image unique : elle sert a la fois d'orchestrateur et de sonde reseau.
# Les conteneurs de sonde sont crees par l'orchestrateur a partir de cette
# meme image, il n'y a donc qu'une seule image a publier ou construire.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      curl fping iputils-ping iperf3 dnsutils iproute2 \
      ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir docker==7.1.0 requests==2.32.3 PyYAML==6.0.2

WORKDIR /app
COPY probe/probe.py /app/probe.py
COPY orchestrator /app/orchestrator
COPY bench.yaml /app/bench.yaml

ENV PYTHONUNBUFFERED=1 \
    BENCH_RESULTS=/app/results \
    BENCH_MODE=smoke

EXPOSE 8888
CMD ["python", "-u", "-m", "orchestrator.main"]
