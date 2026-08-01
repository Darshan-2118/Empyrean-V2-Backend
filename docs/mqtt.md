# Empyrean — MQTT Topic Schema

This document describes the MQTT topics exchanged between Empyrean devices, the broker, the backend, and downstream subscribers. It is the reference for the device-to-backend contract used by the ingestion pipeline.

> **Status:** MQTT ingestion is planned (Phase 4); the `mqtt/` package is currently empty. This document describes the intended device-to-backend contract.

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
