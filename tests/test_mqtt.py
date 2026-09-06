"""MQTT publisher is a no-op without MQTT_HOST."""
import os

from paperwhisper.config import Config
from paperwhisper import mqttpub


def test_mqtt_disabled_without_host():
    os.environ.pop("MQTT_HOST", None)
    cfg = Config()
    assert cfg.mqtt_host == ""
    assert mqttpub.start(cfg) is None


def test_mqtt_config_from_env():
    os.environ["MQTT_HOST"] = "mqtt.example"
    os.environ["MQTT_PORT"] = "1883"
    os.environ["MQTT_USER"] = "paperwhisper"
    os.environ["MQTT_PREFIX"] = "paperwhisper"
    try:
        cfg = Config()
        assert cfg.mqtt_host == "mqtt.example"
        assert cfg.mqtt_port == 1883
        assert cfg.mqtt_user == "paperwhisper"
        assert cfg.mqtt_prefix == "paperwhisper"
    finally:
        for k in ("MQTT_HOST", "MQTT_PORT", "MQTT_USER", "MQTT_PREFIX"):
            os.environ.pop(k, None)
