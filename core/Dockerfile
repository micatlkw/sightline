FROM ultralytics/ultralytics:latest-cpu

WORKDIR /app

# System deps & dynamic privilege-drop utility
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    gosu \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install deterministic Python dependencies (layer cached)
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock

# Application source & package metadata
COPY pyproject.toml .
COPY app/ ./app/
RUN pip install --no-cache-dir --no-deps .

# Non-root dynamic privilege drop entrypoint
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

EXPOSE 8000 8080

ENV YOLO_CONFIG_DIR=/models/.config/Ultralytics
ENV TORCH_HOME=/models/torch

ENTRYPOINT ["/app/entrypoint.sh"]

CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "*", \
     "--log-level", "info"]
