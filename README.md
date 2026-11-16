# Power Sense

Energy use and cost per device in Home Assistant – **measured** from a power sensor, **estimated** for lights and switches without a meter, or both added together.

- One device per thing you care about (washing machine, server rack, "all lamps" …) with power, energy (total, today, this month, this year) and cost sensors.
- **Measured**: pick one or more power sensors (W or kW), for example smart plugs. Several sensors are added up.
- **Estimated**: pick lights, switches or fans and tell Power Sense how many watts they draw. Dimmed lights count proportionally to their brightness. Good enough to see what your lighting costs without buying a plug for every lamp.
- **Both in one device**: e.g. "Lamps" = two metered plugs + 20 estimated bulbs.
- An **overview device** adds up all devices.
- The *Energy* sensor of every device works in the Home Assistant **Energy dashboard** (individual devices).
- **Import history**: fills the past from your recorder, so the charts are not empty on day one.
- Everything is set up in the UI. No YAML, no helpers.

## Installation

### HACS (recommended)

1. HACS → ⋮ → *Custom repositories* → `https://github.com/dermitchi/ha-power-sense`, category *Integration*.
2. Install **Power Sense** and restart Home Assistant.
3. *Settings → Devices & services → Add integration → Power Sense* and enter your electricity price per kWh.

### Manual

Copy `custom_components/power_sense` into your Home Assistant `config/custom_components/` folder and restart.

## Adding devices

*Settings → Devices & services → Power Sense → Add device.*

| Field | Meaning |
| --- | --- |
| Name | Name of the device, e.g. `Washing machine` |
| Power sensors | Sensors with device class *power* (W or kW). Several are added up. |
| Entities to estimate | Lights, switches, fans or input booleans without a meter. |

If you selected entities, the next step asks for:

| Field | Meaning |
| --- | --- |
| Default watts | Power assumed per entity while it is on. |
| Scale with brightness | Dimmed lights count proportionally, but at least 10 %. |
| Watts per entity | Optional, own wattage per entity, as YAML: `light.desk: 9` |

**Many plugs at once:** use *Add several measured devices*, tick all power sensors you want and every one becomes its own device (named after the device it belongs to; sensors already in use are skipped).

You can change everything later with *⋮ → Reconfigure* on the device, and the price under *Configure* of the integration.

**Typical wattages** (full brightness): LED bulb 5–10 W, LED spot 5–6 W, Hue Play 7 W, LED strip 10–25 W. The manufacturer's data sheet is the best source. Estimates are rough (±30 %) – the sensors are named like measured ones, so keep that in mind.

## Entities

For every device:

| Entity | Description |
| --- | --- |
| Power | Current power in W |
| Energy | Total energy in kWh (`total_increasing`, use it in the Energy dashboard) |
| Energy today / this month / this year | kWh in the current period |
| Cost today / this month / this year | Energy × price |

The *Power Sense* device has the same sensors (except the total) summed over all devices.

Totals are integrated over time and survive restarts. If Home Assistant is down for longer than 15 minutes, that gap is not counted.

## Import history

Run **Power Sense: Import history** (*Developer tools → Actions*) once after adding a device:

```yaml
action: power_sense.import_history
data:
  device: Washing machine   # optional, empty = all devices
  days: 60
```

- *Measured* sensors are imported from the **long-term statistics** (hourly mean power), so this reaches back as far as your statistics do – it needs the sensor to have a `state_class` (all normal power sensors do).
- *Estimated* entities are imported from the **recorder state history**, which Home Assistant keeps for 10 days by default (`purge_keep_days`).
- Running it again replaces the previous import, it does not add up.

## Dashboard example

The sensors are normal entities, use any card. For example:

```yaml
type: entities
title: Power
entities:
  - entity: sensor.power_sense_power
  - entity: sensor.power_sense_cost_today
  - entity: sensor.power_sense_cost_this_month
  - entity: sensor.washing_machine_cost_this_month
```

(The exact entity ids depend on your language and device names.)

## Troubleshooting

- **A device shows 0 W**: the source sensor is `unavailable` or not numeric. Power Sense counts such sources as 0 W.
- **A lamp is not counted**: check that the entity is selected under *Entities to estimate* and reports `on`.
- **Wrong currency**: Power Sense uses the currency from *Settings → System → General*.

## Development

```bash
python -m venv .venv && .venv/bin/pip install pytest-homeassistant-custom-component
.venv/bin/pytest -q
```

MIT licensed.
