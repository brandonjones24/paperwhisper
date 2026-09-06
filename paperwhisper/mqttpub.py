"""Publish paperwhisper sync events to an MQTT broker (Home Assistant).

This is a *client* to your existing broker (e.g. HA Mosquitto). It is not
rmfakecloud's tablet MQTT. Disabled unless MQTT_HOST is set.

Uses Home Assistant MQTT discovery so sensors appear under one device
without editing configuration.yaml.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

log = logging.getLogger("paperwhisper.mqtt")

_publisher: "MqttPublisher | None" = None


def start(cfg) -> "MqttPublisher | None":
    global _publisher
    if not getattr(cfg, "mqtt_host", ""):
        return None
    _publisher = MqttPublisher(cfg)
    _publisher.start()
    return _publisher


def emit(event: dict[str, Any]) -> None:
    if _publisher is not None:
        _publisher.publish_state(event)


class MqttPublisher:
    def __init__(self, cfg):
        self.host = cfg.mqtt_host
        self.port = cfg.mqtt_port
        self.user = cfg.mqtt_user
        self.password = cfg.mqtt_password
        self.prefix = cfg.mqtt_prefix.rstrip("/")
        self.discovery = cfg.mqtt_discovery.rstrip("/")
        self.direction = cfg.direction
        self._client = None
        self._lock = threading.Lock()
        self._online = False
        self.client_id = f"paperwhisper-{self.direction}"
        self.avail_topic = f"{self.prefix}/{self.direction}/availability"
        self.state_topic = f"{self.prefix}/{self.direction}/state"

    def start(self) -> None:
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            log.error("paho-mqtt is not installed; MQTT disabled")
            return

        kwargs = {"client_id": self.client_id}
        if hasattr(mqtt, "CallbackAPIVersion"):
            kwargs["callback_api_version"] = mqtt.CallbackAPIVersion.VERSION2
        client = mqtt.Client(**kwargs)
        if self.user:
            client.username_pw_set(self.user, self.password or None)
        client.will_set(self.avail_topic, "offline", qos=1, retain=True)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        self._client = client
        try:
            client.connect(self.host, self.port, keepalive=60)
        except Exception as e:  # noqa: BLE001
            log.error("MQTT connect to %s:%s failed: %s", self.host, self.port, e)
            return
        client.loop_start()
        log.info("MQTT client started -> %s:%s as %s", self.host, self.port, self.client_id)

    def _on_connect(self, client, _userdata, _flags, reason_code, *_extra):
        rc = reason_code
        if hasattr(reason_code, "value"):
            rc = reason_code.value
        if rc != 0:
            log.error("MQTT connection refused rc=%s", reason_code)
            self._online = False
            return
        self._online = True
        log.info("MQTT connected")
        client.publish(self.avail_topic, "online", qos=1, retain=True)
        self._publish_discovery(client)

    def _on_disconnect(self, _client, _userdata, *_args):
        self._online = False
        log.warning("MQTT disconnected")

    def _node_id(self) -> str:
        return f"paperwhisper_{self.direction}"

    def _publish_discovery(self, client) -> None:
        device = {
            "identifiers": ["paperwhisper"],
            "name": "paperwhisper",
            "manufacturer": "homelab",
            "model": "reMarkable ↔ Audiobookshelf",
        }
        avail = {"topic": self.avail_topic, "payload_available": "online",
                 "payload_not_available": "offline"}
        sensors = [
            {
                "object_id": f"{self.direction}_book",
                "name": f"paperwhisper {self.direction} book",
                "unique_id": f"paperwhisper_{self.direction}_book",
                "value_template": "{{ value_json.title }}",
                "icon": "mdi:book-open-page-variant",
            },
            {
                "object_id": f"{self.direction}_progress",
                "name": f"paperwhisper {self.direction} progress",
                "unique_id": f"paperwhisper_{self.direction}_progress",
                "value_template": "{{ value_json.progress_pct }}",
                "unit_of_measurement": "%",
                "icon": "mdi:percent",
                "state_class": "measurement",
            },
            {
                "object_id": f"{self.direction}_updates",
                "name": f"paperwhisper {self.direction} last updates",
                "unique_id": f"paperwhisper_{self.direction}_updates",
                "value_template": "{{ value_json.updates }}",
                "icon": "mdi:sync",
                "state_class": "measurement",
            },
        ]
        for s in sensors:
            topic = f"{self.discovery}/sensor/{self._node_id()}/{s['object_id']}/config"
            payload = {
                "name": s["name"],
                "unique_id": s["unique_id"],
                "state_topic": self.state_topic,
                "json_attributes_topic": self.state_topic,
                "value_template": s["value_template"],
                "availability": [avail],
                "device": device,
                "icon": s["icon"],
            }
            if "unit_of_measurement" in s:
                payload["unit_of_measurement"] = s["unit_of_measurement"]
            if "state_class" in s:
                payload["state_class"] = s["state_class"]
            client.publish(topic, json.dumps(payload), qos=1, retain=True)

    def publish_state(self, event: dict[str, Any]) -> None:
        if self._client is None or not self._online:
            return
        body = {
            "direction": self.direction,
            "title": event.get("title") or "",
            "progress_pct": event.get("progress_pct"),
            "updates": event.get("updates", 0),
            "dry_run": bool(event.get("dry_run")),
            "detail": event.get("detail") or "",
        }
        payload = json.dumps(body)
        with self._lock:
            self._client.publish(self.state_topic, payload, qos=1, retain=True)
