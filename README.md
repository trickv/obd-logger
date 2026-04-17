# obd-logger

A Raspberry Pi-based OBD-II data logger that runs in your car. It connects to
an ELM327 Bluetooth adapter, logs sensor data to CSV files, and reports status
to Home Assistant.

## System Architecture

![System Architecture](doc/architecture.svg)

<details>
<summary>Diagram source (Mermaid)</summary>

```mermaid
graph TB
    subgraph Car
        ELM[ELM327 Bluetooth<br/>OBD-II Adapter]
        OBD[Car OBD-II Port]
        OBD --- ELM

        subgraph RPi[Raspberry Pi + PiSugar3 Battery]
            logger[obd-logger<br/>OBD monitor loop]
            csv[(~/log/obd-*.csv<br/>sensor data)]
            events[(~/log/events.jsonl<br/>event log)]
            dtc[(~/log/dtc.log<br/>trouble codes)]
            post[post-to-hass<br/>every 1 min]
            upd[update<br/>every 1 min<br/>15 min cooldown]
            sync[sync-data<br/>rsync]
            onboot[onboot<br/>BT bind + launch]
            systemd[obd.service<br/>systemd unit]
        end

        ELM -- "Bluetooth RFCOMM<br/>38400 baud" --> logger
        logger --> csv
        logger --> events
        logger --> dtc
        systemd -- starts --> onboot
        onboot -- starts --> logger
        upd -- calls --> sync
    end

    subgraph Home Server
        rsync_srv[rsync server<br/>Docker container]
        importer[import-to-sqlite<br/>inotify watcher]
        sqlite[(obd.sqlite)]
        grafana[Grafana<br/>+ SQLite plugin]
        hass[Home Assistant]

        rsync_srv -- "CSV files<br/>delivered" --> importer
        importer --> sqlite
        sqlite --> grafana
    end

    sync -- "rsync over Tailscale<br/>any network" --> rsync_srv
    post -- "HTTPS<br/>when on WiFi" --> hass
    upd -- "git pull<br/>when on WiFi" --> GitHub[(GitHub)]
```

</details>

The Pi lives in the car and is **offline during drives**. Data collection and
data processing are fully decoupled:

1. **Driving** — Pi is powered by the car, logs OBD-II sensor data to CSV files
   and operational events to a JSONL event log. No network required.
2. **Car off** — Pi detects the engine shutting down, syncs logs, and schedules
   a shutdown (with a 15-minute grace period in case the car restarts).
3. **Network available** — whenever the Pi has any network connection, cron
   syncs all logs to a file server via rsync over Tailscale. Grafana reads
   the data from there.

All logs live in `~/log/` and are designed for post-mortem analysis. The event
log (`events.jsonl`) captures every state transition — startup, connection,
car-on, car-off, shutdown scheduled/cancelled, crashes with tracebacks — so
you can reconstruct what happened even in weird sequences like the car turning
off and back on 30 seconds later.

## Known Limitations

- **First ~60 seconds of driving are missed.** The Pi boots from the PiSugar3
  battery when the car powers on, but boot + Bluetooth pairing + OBD
  connection takes about a minute. The car is already moving before the first
  sensor reading is logged.

- **No real-time streaming.** Sync runs via rsync over Tailscale, so it works
  from any network the Pi can reach — not just home WiFi — but it's still
  batch delivery every minute via cron, not live telemetry. Fine for the
  "review drives later" use case; not suitable for live tracking.

## What it logs

- **Sensor CSV** (`obd-YYYYMMDD-HHMMSS.csv`) — one file per drive with RPM,
  speed, coolant temp, engine load, throttle, intake temp, MAF, timing advance,
  fuel level, run time, barometric pressure, ELM voltage, PiSugar battery
- **Event log** (`events.jsonl`) — structured operational events for debugging
- **DTC log** (`dtc.log`) — diagnostic trouble codes (check engine light codes)

## Grafana

![Grafana OBD Drive Detail dashboard](doc/grafana-drive-detail.png)

[Live example dashboard snapshot](https://snapshots.raintank.io/dashboard/snapshot/5QPIwr2gFuFneH95Gw4PRxBVjHdcNJSC?orgId=0&from=2026-04-16T17:17:45.678Z&to=2026-04-16T17:40:45.641Z&timezone=browser&var-drive_id=obd-20260416-121844-001DA5072049)

## Hardware

- Raspberry Pi with Raspberry Pi OS (Trixie)
- PiSugar3 battery module (keeps the Pi alive briefly after the car turns off)
- ELM327 Bluetooth OBD-II adapter

## Setup

See [INSTALL.md](INSTALL.md) for full installation instructions.
