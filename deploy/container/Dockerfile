# RoadSense demo image: the whole stack in one container, seeded with the keyless demo trips on first start.
# Built by scripts/deploy_gcp.py (a VM behind Caddy) or scripts/deploy_hf.py (a Hugging Face Space), which stage
# this file with the code, the demo trip and the two detector weights. The web app listens on 7860; the agent API
# (8000) and the mock 311 (8311) are only reachable from inside the container network.
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 curl \
    && rm -rf /var/lib/apt/lists/*
# Spaces run the container as uid 1000
RUN useradd -m -u 1000 user
WORKDIR /app

COPY requirements.lock .
RUN pip install --no-cache-dir torch==2.14.1 torchvision==0.29.2 --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r requirements.lock

COPY --chown=user roadsense ./roadsense
COPY --chown=user scripts ./scripts
COPY --chown=user demo ./demo
COPY --chown=user weights ./weights
COPY --chown=user pyproject.toml start.sh ./

ENV HOME=/home/user PYTHONUNBUFFERED=1 YOLO_CONFIG_DIR=/tmp/ultralytics \
    ROADSENSE_WEIGHTS_DIR=/app/weights ROADSENSE_DATA_DIR=/app/data/demo ROADSENSE_DEVICE=cpu \
    ROADSENSE_COOKIE_SECURE=1 ROADSENSE_OFFLINE_GEO=1
RUN mkdir -p /app/data && chown -R user /app
USER user
EXPOSE 7860
CMD ["sh", "start.sh"]
