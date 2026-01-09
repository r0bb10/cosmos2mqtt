FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Timezone
ENV TZ=Europe/Rome
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# Embed build commit into image (set by CI via --build-arg COMMIT_SHA). Default 'local' for local builds.
ARG COMMIT_SHA=local
ENV COMMIT_SHA=${COMMIT_SHA}
LABEL org.opencontainers.image.revision=${COMMIT_SHA}

ENV PYTHONUNBUFFERED=1
ENV DEOS_HOST=localhost
ENV MQTT_BROKER=localhost
ENV MQTT_USER=user
ENV MQTT_PASS=password
ENV HEALTH_PORT=8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 CMD ["python", "-c", "import urllib.request,sys;\ntry:\n r=urllib.request.urlopen('http://127.0.0.1:8080/ready',timeout=5); sys.exit(0 if r.status==200 else 1)\nexcept Exception:\n sys.exit(1)"]

CMD ["python", "app.py"]
