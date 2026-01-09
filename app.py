#!/usr/bin/env python3
"""
DEOS Simple Monitor
"""
import argparse
import json
import logging
import os
import re
import struct
import threading
import time
from datetime import datetime
from typing import Dict, Optional
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

import paho.mqtt.client as mqtt
import requests

DEOS_HOST = os.getenv("DEOS_HOST", "localhost")
MQTT_BROKER = os.getenv("MQTT_BROKER", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER = os.getenv("MQTT_USER")
MQTT_PASS = os.getenv("MQTT_PASS")
AVAILABILITY_TOPIC = "cosmos2mqtt/availability"

# Health HTTP server port
HEALTH_PORT = int(os.getenv("HEALTH_PORT", "8080"))

# Parse exposed IDs filter (comma-separated IDs like "AI00,AI01,DO02" or empty for all)
EXPOSED_IDS_RAW = os.getenv("EXPOSED_IDS", "")
EXPOSED_IDS = set(
    eid.strip().upper() for eid in EXPOSED_IDS_RAW.split(",") if eid.strip()
) if EXPOSED_IDS_RAW else None  # None means expose all

# DEBUG env controls whether per-poll sensor values are logged (yes/no, default no)
DEBUG_RAW = os.getenv("DEBUG", "no")
DEBUG_ENABLED = str(DEBUG_RAW).strip().lower() in ("1", "true", "yes", "y")

ANALOG_INPUT_PAYLOAD = """0;SPS_MERK;30;932;
1;SPS_VARI;60;4512;
2;SPS_VARI;108;4610;
3;SPS_VARI;8;4670;
4;SPS_VARI;108;4870;
5;SPS_VARI;4;4924;"""

DIGITAL_OUTPUT_PAYLOAD = """0;SPS_MERK;109;1045;
1;SPS_MERK;4;1156;"""

ANALOG_INPUT_MAP = {
    0: ('AI00', 'sonda mandata climatizzazione'),
    2: ('AI01', 'sonda ritorno climatizzazione'),
    3: ('AI02', 'sonda accumulo climatizzazione'),
    5: ('AI03', 'sonda ritorno carico boyler'),
    6: ('AI04', 'sonda accumulo ACS'),
    8: ('AI05', 'sonda ricircolo ACS'),
    9: ('AI06', 'sonda primario scambiatore CLIMATIZZ.'),
    11: ('AI07', 'sonda secondario scambiatore CLIMATIZZ.')
}

# ============================================================================
# DIGITAL OUTPUT DECODING - Theory and Implementation Notes
# ============================================================================
# The digital output response is a BINARY STRING (literal '0' and '1' chars),
# NOT hex-encoded like analog inputs.
#
# DISCOVERED PATTERN (empirically tested):
#   - Line 0: DO00-DO09 at bit positions 163, 169, 175, 181, 187, 193, 199, 205, 211, 217
#   - Line 1: DO10-DO11 at bit positions 1, 7
#   - Spacing: 6 bits apart (suggests 1 bit for state + 5 bits metadata per output)
#
# EVIDENCE:
#   - When DO00, DO01, DO02 were ON: positions [1, 33, 163, 169, 175] had '1'
#   - When DO02 turned OFF: position 175 changed from '1' to '0'
#   - When DO03 was toggled ON in tests: position 181 changed from '0' to '1'
#     (sample captures show DO00,DO01,DO03 ON while DO02 was OFF — 175=0, 181=1)
#   - Positions 1 and 33 appear to be other system flags (not DO00-DO11)
#
# ASSUMPTIONS & UNCERTAINTIES:
#   - DO00-DO03 have been empirically tested and follow the 6-bit spacing
#     pattern; DO04-DO11 remain extrapolated from that pattern
#   - Unknown what the 5 bits between outputs represent (status? metadata?)
#   - Line 1 positions (1,7) for DO10-DO11 follow same spacing but untested
#
# FOR FUTURE REFACTORING:
#   - If any DO03-DO11 don't respond correctly, the spacing pattern may be wrong
#   - Consider extracting the 5 metadata bits if they contain useful info
#   - The payload offsets (1045, 1156) might map to device memory addresses
# ============================================================================

DIGITAL_OUTPUT_MAP = {
    0: ('DO00', 'abilitazione PdC 1'),
    1: ('DO01', 'abilitazione PdC 2'),
    2: ('DO02', 'comando EP 1 Climatizz.'),
    3: ('DO03', 'comando EP 2 Climatizz.'),
    4: ('DO04', 'reserve'),
    5: ('DO05', 'reserve'),
    6: ('DO06', 'reserve'),
    7: ('DO07', 'reserve'),
    8: ('DO08', 'reserve'),
    9: ('DO09', 'reserve'),
    10: ('DO10', 'reserve'),
    11: ('DO11', 'reserve')
}

logger = logging.getLogger("cosmos2mqtt")


def setup_logging(debug: bool) -> None:
    level = logging.DEBUG if debug else logging.INFO
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.handlers = []
    root.setLevel(level)
    root.addHandler(handler)


def parse_hex_floats(hex_string: str) -> list[Optional[float]]:
    values: list[Optional[float]] = []
    for i in range(0, len(hex_string), 8):
        chunk = hex_string[i:i + 8]
        if len(chunk) != 8:
            continue
        try:
            values.append(struct.unpack('>f', bytes.fromhex(chunk))[0])
        except (ValueError, struct.error):
            values.append(None)
    return values


class CosmosClient:
    MAX_RETRIES = 3
    RETRY_DELAY = 2

    def __init__(self, host: str) -> None:
        self.host = host
        self.session = requests.Session()
        self.connected = False
        self.system_info: Optional[Dict[str, str]] = None
        # timestamp of last successful poll (seconds since epoch)
        self.last_poll: Optional[float] = None

    def _fetch_system_info(self) -> Dict[str, str]:
        """Fetch system information from sysinfo.htm endpoint."""
        default_info = {
            'model': 'COSMOS 600',
            'manufacturer': 'DEOS.AG',
            'sw_version': 'Unknown',
            'serial_number': 'Unknown',
            'hw_version': 'Unknown',
            'mac_address': 'Unknown'
        }
        
        try:
            url = f"http://{self.host}:3000/sysinfo.htm"
            response = self.session.get(url, timeout=5)
            response.raise_for_status()
            html = response.text
            
            # Parse HTML table for system info
            info = {}
            if match := re.search(r'<tr><td>Hardware type</td><td>([^<]+)</td>', html):
                info['model'] = match.group(1).strip()
            if match := re.search(r'<tr><td>Hardware version</td><td>([^<]+)</td>', html):
                info['hw_version'] = match.group(1).strip()
            if match := re.search(r'<tr><td>Firmware version</td><td>([^<]+)</td>', html):
                info['sw_version'] = match.group(1).strip()
            if match := re.search(r'<tr><td>Serial number</td><td>([^<]+)</td>', html):
                info['serial_number'] = match.group(1).strip()
            if match := re.search(r'<tr><td>MAC address</td><td>([^<]+)</td>', html):
                info['mac_address'] = match.group(1).strip()
            
            info['manufacturer'] = 'DEOS.AG'
            
            # Use defaults for any missing values
            result = {**default_info, **info}
            logger.info("Fetched device info: %s %s (S/N: %s, FW: %s)", 
                       result['manufacturer'], result['model'], 
                       result['serial_number'], result['sw_version'])
            return result
            
        except Exception as exc:
            logger.warning("Failed to fetch system info from %s:3000/sysinfo.htm: %s (using defaults)", 
                         self.host, exc)
            return default_info

    def _log_connected(self) -> None:
        if not self.connected:
            logger.info("Connected to COSMOS (%s)", self.host)
            self.connected = True

    def _log_reconnect(self, attempt: int, error: Exception, label: str) -> None:
        logger.warning("Reconnecting to COSMOS (%s attempt %d): %s", label, attempt, error)
        self.connected = False

    def _post(self, payload: str, label: str) -> Optional[str]:
        url = f"http://{self.host}/cgi-bin/cosmobdf.cgi?function=25&t={int(time.time() * 1000)}"
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                response = self.session.post(url, data=payload, timeout=5)
                response.raise_for_status()
                text = response.text.strip()
                if not text.startswith("OK"):
                    raise requests.RequestException("Unexpected COSMOS response")
                self._log_connected()
                return text
            except requests.RequestException as exc:  # pragma: no cover - external call
                self._log_reconnect(attempt, exc, label)
                if attempt < self.MAX_RETRIES:
                    time.sleep(self.RETRY_DELAY)
        return None

    def get_all_sensors(self) -> Optional[Dict[str, Dict[str, float]]]:
        text = self._post(ANALOG_INPUT_PAYLOAD, "analog")
        if not text:
            return None

        sensors: Dict[str, Dict[str, float]] = {}
        for line in text.split("\n"):
            if not line.startswith("4;"):
                continue
            parts = line.split(";")
            if len(parts) < 5:
                continue
            floats = parse_hex_floats(parts[4])
            for index, (sensor_id, sensor_name) in ANALOG_INPUT_MAP.items():
                if index < len(floats) and floats[index] is not None:
                    value = floats[index]
                    if -50 < value < 150:
                        sensors[sensor_id] = {
                            'name': sensor_name,
                            'value': value
                        }
        if sensors:
            self.last_poll = time.time()
            return sensors
        return None

    def get_digital_outputs(self) -> Optional[Dict[str, Dict[str, object]]]:
        text = self._post(DIGITAL_OUTPUT_PAYLOAD, "digital")
        if not text:
            return None

        outputs: Dict[str, Dict[str, object]] = {}
        line_states: Dict[int, bool] = {}
        for line in text.split("\n"):
            if not line or line == "OK":
                continue
            parts = line.split(";")
            if len(parts) < 5:
                continue
            try:
                line_number = int(parts[0])
            except ValueError:
                continue
            binary_data = parts[4]
            if line_number == 0:
                for i in range(10):
                    pos = 163 + (i * 6)
                    line_states[i] = (pos < len(binary_data) and binary_data[pos] == '1')
            elif line_number == 1:
                line_states[10] = (len(binary_data) > 1 and binary_data[1] == '1')
                line_states[11] = (len(binary_data) > 7 and binary_data[7] == '1')

        for idx, state in line_states.items():
            if idx in DIGITAL_OUTPUT_MAP:
                output_id, output_name = DIGITAL_OUTPUT_MAP[idx]
                outputs[output_id] = {
                    'name': output_name,
                    'state': state
                }
        if outputs:
            self.last_poll = time.time()
            return outputs
        return None


class HealthStatus:
    """Shared health state used by the lightweight HTTP health endpoints."""
    def __init__(self) -> None:
        self.daemon_started: bool = False
        self.mqtt_manager: Optional[MQTTManager] = None
        self.cosmos: Optional[CosmosClient] = None


def start_health_server(health_status: HealthStatus, port: int) -> threading.Thread:
    """Start a small threaded HTTP server exposing /health and /ready.

    - /health: liveness (daemon responding)
    - /ready: readiness (MQTT connected and recent COSMOS poll)
    """

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split('?', 1)[0]
            if path == '/health':
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                payload = {'status': 'ok', 'daemon_started': bool(health_status.daemon_started)}
                self.wfile.write(json.dumps(payload).encode('utf-8'))
                return

            if path == '/ready':
                ready = False
                details = {'mqtt_connected': False, 'last_cosmos_poll': None, 'poll_interval': None}
                if health_status.mqtt_manager is not None:
                    details['mqtt_connected'] = bool(getattr(health_status.mqtt_manager, 'connected', False))
                if health_status.cosmos is not None and getattr(health_status.cosmos, 'last_poll', None):
                    lp = health_status.cosmos.last_poll
                    details['last_cosmos_poll'] = lp
                    try:
                        interval = int(os.getenv('POLL_INTERVAL', '5'))
                    except Exception:
                        interval = 5
                    age = time.time() - lp
                    details['poll_interval'] = interval
                    if details['mqtt_connected'] and age <= (2 * interval):
                        ready = True

                self.send_response(200 if ready else 503)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'ready': ready, 'details': details}).encode('utf-8'))
                return

            self.send_response(404)
            self.end_headers()

        def log_message(self, format: str, *args) -> None:  # suppress default logging
            return

    server = ThreadingHTTPServer(('0.0.0.0', port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info('Health server listening on port %s', port)
    return thread


class MQTTManager:
    def __init__(self, host: str, port: int, username: Optional[str], password: Optional[str]) -> None:
        self.host = host
        self.port = port
        self.connected = False
        self.connection_event = threading.Event()
        
        client_id = f"cosmos2mqtt-{int(time.time())}"
        kwargs = {"client_id": client_id}
        
        # Fix: paho-mqtt 2.1.0 uses CallbackAPIVersion.VERSION2 (not V2, V3, etc)
        if hasattr(mqtt, "CallbackAPIVersion"):
            kwargs["callback_api_version"] = mqtt.CallbackAPIVersion.VERSION2
        
        self.client = mqtt.Client(**kwargs)
        
        if username and password:
            self.client.username_pw_set(username, password)
        
        # Set Last Will and Testament for proper availability
        self.client.will_set(AVAILABILITY_TOPIC, "offline", qos=1, retain=True)
        
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_publish = self._on_publish
        self.client.reconnect_delay_set(1, 60)

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            self.connected = True
            self.connection_event.set()
            logger.info("Connected to MQTT (%s)", self.host)
            # Publish availability immediately on connection
            self.client.publish(AVAILABILITY_TOPIC, "online", qos=1, retain=True)
        else:
            logger.warning("MQTT connection failed (rc=%s)", rc)
            self.connected = False

    def _on_disconnect(self, client, userdata, rc, properties=None):
        if rc != 0:
            logger.warning("Disconnected from MQTT (rc=%s), reconnecting...", rc)
        self.connected = False
        self.connection_event.clear()
    
    def _on_publish(self, client, userdata, mid, reason_code=None, properties=None):
        logger.debug("MQTT message published (mid=%s)", mid)

    def start(self) -> None:
        while True:
            try:
                self.client.connect(self.host, self.port, keepalive=60)
                break
            except Exception as exc:  # pragma: no cover - external
                logger.warning("Failed to connect to MQTT: %s (retrying in 5s)", exc)
                time.sleep(5)
        
        self.client.loop_start()
        
        # Wait for connection to be established (max 30 seconds)
        if not self.connection_event.wait(timeout=30):
            logger.error("MQTT connection timeout after 30 seconds")

    def publish(self, topic: str, payload: str, retain: bool = False, qos: int = 1, force: bool = False) -> bool:
        if not force and not self.connected:
            logger.debug("Skipping publish to %s (not connected)", topic)
            return False
        try:
            result = self.client.publish(topic, payload, qos=qos, retain=retain)
            result.wait_for_publish(timeout=5)
            logger.debug("Published to %s: %s (retain=%s)", topic, payload[:100] if len(payload) > 100 else payload, retain)
            return True
        except Exception as exc:  # pragma: no cover - external
            logger.warning("MQTT publish error to %s: %s", topic, exc)
            return False

    def stop(self) -> None:
        logger.info("Shutting down MQTT connection")
        if self.connected:
            self.publish(AVAILABILITY_TOPIC, "offline", retain=True, force=True)
            time.sleep(0.5)  # Give time for offline message to send
        try:
            self.client.disconnect()
        except Exception:
            pass
        self.client.loop_stop()


class HomeAssistantPublisher:
    def __init__(self, mqtt_manager: MQTTManager, system_info: Dict[str, str]) -> None:
        self.mqtt = mqtt_manager
        # Build device info from scraped system information
        serial = system_info.get('serial_number', 'unknown')
        model = system_info.get('model', 'COSMOS')
        self.device_info = {
            'identifiers': [f"cosmos2mqtt_{serial}"],
            'name': f"{system_info.get('manufacturer', 'DEOS')} {model}",
            'model': model,
            'manufacturer': system_info.get('manufacturer', 'DEOS.AG'),
            'sw_version': system_info.get('sw_version', 'Unknown'),
            'serial_number': serial,
            'hw_version': system_info.get('hw_version', 'Unknown'),
            'configuration_url': f"http://{DEOS_HOST}:3000"
        }
        self.configured_topics: set[str] = set()

    def _normalize(self, raw: str) -> str:
        return raw.lower().replace(' ', '_')

    def _config_topic(self, component: str, entity_id: str) -> str:
        return f"homeassistant/{component}/{entity_id}/config"

    def _state_topic(self, component: str, entity_id: str) -> str:
        return f"cosmos2mqtt/{component}/{entity_id}/state"

    def _publish_discovery(self, component: str, entity_id: str, payload: dict) -> None:
        config_topic = self._config_topic(component, entity_id)
        serialized = json.dumps(payload)
        if self.mqtt.publish(config_topic, serialized, retain=True):
            self.configured_topics.add(config_topic)

    def publish_analog_sensors(self, sensors: Dict[str, Dict[str, float]]) -> None:
        component = 'sensor'
        configured_ids = []
        
        for sensor_id, data in sensors.items():
            # Filter based on EXPOSED_IDS
            if EXPOSED_IDS is not None and sensor_id.upper() not in EXPOSED_IDS:
                continue
            
            normalized_id = self._normalize(sensor_id)
            entity_id = f"cosmos_{normalized_id}"
            state_topic = self._state_topic(component, entity_id)
            
            # Home Assistant discovery payload
            payload = {
                'name': data['name'].title(),
                'unique_id': f"cosmos2mqtt_{normalized_id}",
                'default_entity_id': f"sensor.cosmos_{normalized_id}",
                'state_topic': state_topic,
                'unit_of_measurement': '°C',
                'device_class': 'temperature',
                'state_class': 'measurement',
                'device': self.device_info,
                'availability_topic': AVAILABILITY_TOPIC,
                'availability_mode': 'latest'
            }
            
            # Publish discovery config (only once per entity)
            config_topic = self._config_topic(component, entity_id)
            if config_topic not in self.configured_topics:
                self._publish_discovery(component, entity_id, payload)
                configured_ids.append(sensor_id)
            
            # Publish state
            formatted = f"{data['value']:.1f}"
            if self.mqtt.publish(state_topic, formatted, retain=True):
                logger.debug("Updated %s: %s°C", sensor_id, formatted)
        
        # Log all configured sensors in a single line
        if configured_ids:
            logger.info("Configured sensors: %s", ", ".join(configured_ids))

    def publish_binary_sensors(self, outputs: Dict[str, Dict[str, object]]) -> None:
        component = 'binary_sensor'
        configured_ids = []
        
        for output_id, data in outputs.items():
            # Filter based on EXPOSED_IDS
            if EXPOSED_IDS is not None and output_id.upper() not in EXPOSED_IDS:
                continue
            
            normalized_id = self._normalize(output_id)
            entity_id = f"cosmos_{normalized_id}"
            state_topic = self._state_topic(component, entity_id)
            
            # Home Assistant discovery payload
            payload = {
                'name': data['name'].title(),
                'unique_id': f"cosmos2mqtt_{normalized_id}",
                'default_entity_id': f"binary_sensor.cosmos_{normalized_id}",
                'state_topic': state_topic,
                'payload_on': 'ON',
                'payload_off': 'OFF',
                'device_class': 'power',
                'device': self.device_info,
                'availability_topic': AVAILABILITY_TOPIC,
                'availability_mode': 'latest'
            }
            
            # Publish discovery config (only once per entity)
            config_topic = self._config_topic(component, entity_id)
            if config_topic not in self.configured_topics:
                self._publish_discovery(component, entity_id, payload)
                configured_ids.append(output_id)
            
            # Publish state
            state = 'ON' if data.get('state') else 'OFF'
            if self.mqtt.publish(state_topic, state, retain=True):
                logger.debug("Updated %s: %s", output_id, state)
        
        # Log all configured binary sensors in a single line
        if configured_ids:
            logger.info("Configured binary_sensors: %s", ", ".join(configured_ids))

    def cleanup(self) -> None:
        logger.info("Cleaning up Home Assistant entities")
        # Send offline availability
        self.mqtt.publish(AVAILABILITY_TOPIC, 'offline', retain=True, force=True)
        # Remove discovery configs so entities disappear from HA on shutdown
        topics = list(self.configured_topics)
        removed = 0
        for topic in topics:
            if self.mqtt.publish(topic, '', retain=True, force=True):
                removed += 1
            self.configured_topics.discard(topic)
        logger.info("Removed %d entity discovery configs", removed)

def run_daemon(cosmos: CosmosClient, interval: int) -> None:
    # Start health server early and wire it to our components
    health_status = HealthStatus()
    health_status.cosmos = cosmos
    # start HTTP health server in background
    try:
        start_health_server(health_status, HEALTH_PORT)
    except Exception as exc:
        logger.warning("Failed to start health server on port %s: %s", HEALTH_PORT, exc)

    # Initialize MQTT connection
    mqtt_manager = MQTTManager(MQTT_BROKER, MQTT_PORT, MQTT_USER, MQTT_PASS)
    health_status.mqtt_manager = mqtt_manager
    mqtt_manager.start()
    
    # Ensure MQTT is connected before proceeding
    if not mqtt_manager.connected:
        logger.error("Failed to establish MQTT connection, exiting")
        return
    # At this point MQTT is connected
    # Connect to COSMOS (perform a single poll to establish connection and log it)
    sensors = cosmos.get_all_sensors()
    # Fetch system info after COSMOS connection so logs appear in desired order
    system_info = cosmos._fetch_system_info()
    cosmos.system_info = system_info

    # Create publisher with freshly fetched system info
    publisher = HomeAssistantPublisher(mqtt_manager, cosmos.system_info)
    # Perform initial fetch/publish so discovery configs are sent before continuous loop
    try:
        if sensors:
            publisher.publish_analog_sensors(sensors)
        outputs = cosmos.get_digital_outputs()
        if outputs:
            publisher.publish_binary_sensors(outputs)

        # Log initial poll values if DEBUG enabled
        if DEBUG_ENABLED:
            parts = []
            if sensors:
                sensor_vals = [f"{k}={v['value']:.1f}" for k, v in sensors.items() if EXPOSED_IDS is None or k.upper() in EXPOSED_IDS]
                if sensor_vals:
                    parts.append("sensors: " + ", ".join(sensor_vals))
            if outputs:
                output_vals = [f"{k}={'ON' if v.get('state') else 'OFF'}" for k, v in outputs.items() if EXPOSED_IDS is None or k.upper() in EXPOSED_IDS]
                if output_vals:
                    parts.append("outputs: " + ", ".join(output_vals))
            if parts:
                logger.info("Poll: %s", "; ".join(parts))

            # mark daemon as started (liveness readiness)
            health_status.daemon_started = True
        logger.info("Starting Daemon (polling)")

        iteration = 0
        while True:
            iteration += 1
            # Fetch and publish analog sensors
            sensors = cosmos.get_all_sensors()
            if sensors:
                publisher.publish_analog_sensors(sensors)
            
            # Fetch and publish digital outputs
            outputs = cosmos.get_digital_outputs()
            if outputs:
                publisher.publish_binary_sensors(outputs)
            
            # Log per-poll sensor values when DEBUG enabled
            if DEBUG_ENABLED:
                parts = []
                if sensors:
                    sensor_vals = [f"{k}={v['value']:.1f}" for k, v in sensors.items() if EXPOSED_IDS is None or k.upper() in EXPOSED_IDS]
                    if sensor_vals:
                        parts.append("sensors: " + ", ".join(sensor_vals))
                if outputs:
                    output_vals = [f"{k}={'ON' if v.get('state') else 'OFF'}" for k, v in outputs.items() if EXPOSED_IDS is None or k.upper() in EXPOSED_IDS]
                    if output_vals:
                        parts.append("outputs: " + ", ".join(output_vals))
                if parts:
                    logger.info("Poll: %s", "; ".join(parts))

            time.sleep(interval)
    except KeyboardInterrupt:
        logger.info('Received shutdown signal, stopping daemon')
    finally:
        publisher.cleanup()
        mqtt_manager.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description='DEOS COSMOS monitor')
    parser.add_argument('-t', '--interval', type=int,
                        default=int(os.getenv('POLL_INTERVAL', '5')),
                        help='Polling interval in seconds for headless mode')
    args = parser.parse_args()
    setup_logging(debug=DEBUG_ENABLED)
    cosmos = CosmosClient(DEOS_HOST)
    interval = max(1, args.interval)
    run_daemon(cosmos, interval)


if __name__ == '__main__':
    main()
