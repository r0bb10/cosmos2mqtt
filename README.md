# Cosmos2MQTT

Lightweight bridge that polls a DEOS COSMOS controller, publishes analog temperature probes and digital outputs to MQTT, and exposes them via Home Assistant autodiscovery.

## Quick start

The container runs **headless by default**; its logs are intentionally limited to lifecycle events such as `Connected to COSMOS`, `Connected to MQTT`, and their reconnection attempts so you can easily monitor stability without raw sensor noise. The MQTT broker details, credentials, and DEOS host are controlled via environment variables (see below).

```yaml
services:
  cosmos2mqtt:
    image: ghcr.io/r0bb10/cosmos2mqtt:latest
    container_name: cosmos2mqtt
    restart: unless-stopped
    environment:
      - DEOS_HOST=192.168.1.100
      - MQTT_BROKER=192.168.1.101
      - POLL_INTERVAL=5
      - EXPOSED_IDS=
      - DEBUG=no
```

| Environment variable | Default     | Description |
|----------------------|-------------|-------------|
| `DEOS_HOST`          | `localhost` | DEOS controller hostname or IP |
| `MQTT_BROKER`        | `localhost` | MQTT broker hostname or IP |
| `MQTT_PORT`          | `1883`      | MQTT port |
| `MQTT_USER`          | *(empty)*   | MQTT username (required for authenticated brokers) |
| `MQTT_PASS`          | *(empty)*   | MQTT password (required for authenticated brokers) |
| `POLL_INTERVAL`      | `5`         | Seconds between successive polls in headless mode |
| `EXPOSED_IDS`        | *(empty)*   | Comma-separated entity IDs to expose (e.g., `AI00,AI01,DO02`) |
| `DEBUG`              | `no`        | `yes`/`no` — when `yes` per-poll sensor values are logged to container logs |


## Home Assistant integration

- Analog probes (`AI00`-`AI07`) are published as `sensor` entities with device class `temperature`, 1 decimal precision, and retained states via `cosmos2mqtt/sensor/<entity>/state`.
- Digital outputs (`DO00`-`DO11`) appear as `binary_sensor` entities with payloads `ON`/`OFF` and a `power` device class.
- Each entity publishes a retained discovery payload under `homeassistant/{component}/cosmos_<id>/config` so HA can pick them up automatically.

---

Licensed under the MIT License. See [LICENSE](LICENSE) for details.
