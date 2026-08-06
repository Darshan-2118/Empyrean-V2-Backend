# Empyrean — MQTT Topic Schema

This document describes the MQTT topics exchanged between Empyrean devices, the broker, the backend, and downstream subscribers. It is the reference for the device-to-backend contract used by the ingestion pipeline.

> **Status:** MQTT ingestion is **implemented** (Phase 4). `mqtt/client.py` subscribes to the reading + status topics (QoS 1), `mqtt/validator.py` validates payloads (Pydantic v2), `mqtt/config.py` publishes config changes back to devices, and `python -m mqtt.client` runs the consumer standalone. The alert-broadcast topic (`air/alerts`) is wired in a later phase (WebSocket).

## Client lifecycle (M-10)

The client is wired into the app process lifecycle, gated behind the `MQTT_ENABLED` env flag (default off). When `MQTT_ENABLED` is set to a truthy value, `app.py` starts the client via `start_mqtt()` in `before_serving` and stops it via `stop_mqtt()` in `after_serving`, so ingestion runs inside the API server process. When `MQTT_ENABLED` is unset/disabled (the default), the API runs without a broker connection (M-10).

The client can also be run standalone in its own process (systemd/supervisor):

```
python -m mqtt.client
```

Both paths run a startup subscription smoke check (`MQTTClient.wait_until_ready`) that exits non-zero if the broker does not grant the subscriptions, so an orchestrator can restart it. The full lifecycle is `MQTTClient.start()` then `MQTTClient.stop()` — call these from your process runner, or rely on the `MQTT_ENABLED`-gated hooks in `app.py`.

## Delivery semantics (L-20 / M-8)

The client uses a fixed client id (`empyrean-backend`) with a **persistent session** (`clean_session=False`) and QoS 1 subscriptions, so the broker retains the subscription while the backend is offline and queues QoS 1 device messages for it. Combined with the client's bounded in-memory ingest queue and bounded Celery-dispatch retry (`mqtt/client.py`), delivery is **at-least-once**: a brief broker/Redis/celery outage does not silently discard a telemetry cycle. To avoid dropped messages across a *single backend instance*, set `task_acks_late=True` + `task_reject_on_worker_lost=True` in `celery_app.py`. Running more than one backend with the same fixed client id will share the offline queue rather than delivering every message to every instance.

## MQTT Topic Schema

| Topic | Direction | Payload |
|---|---|---|
| `air/node/{id}/reading` | Device → Broker → Backend | Full sensor reading JSON |
| `air/node/{id}/status` | Device → Broker | Heartbeat: `{ online, battery_v, firmware }` |
| `air/node/{id}/config` | Backend → Device | Remote config: `{ interval_s, fuzzy_enabled }` |
| `air/alerts` | Backend → Subscribers | Alert broadcast: `{ node_id, aqi, category, timestamp }`, bridged to the frontend over WebSocket |

QoS level 1 (at least once) is used for all device publishes.

## Hardware / Firmware (reference only)

The backend consumes data published by ESP32 nodes (MicroPython) fitted with BME680, MQ135, PMS5003, and NEO-6M GPS sensors, which publish JSON readings to `air/node/{id}/reading` over MQTTS every 30 seconds. Firmware details, wiring, and on-device pre-classification are out of scope for this repo — see the hardware/firmware repo for that implementation.

## Related Docs

- [architecture.md](architecture.md)
- [getting-started.md](getting-started.md)
- [README](../README.md)
