# MOSS Control Plane

MOSS Control Plane is the operator interface above Gateway runtime.

Layers:

- Control Plane API: aggregates devices, missions, memory, planner, vision, Home Assistant and bridge status.
- Web UI: consumes Control Plane API only.
- Gateway modules keep ownership of execution and safety.

The first version is read-oriented. Physical actions continue to require existing Mission/Safety flows.

Planned views:

- Dashboard
- Devices
- Missions
- Planner
- Safety
- Vision
- Home
