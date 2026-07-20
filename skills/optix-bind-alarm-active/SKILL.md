---
name: optix-bind-alarm-active
description: Drive an indicator (visibility, color, blink) from an existing alarm's Active state via the live bridge — zero-NetLogic alarm annunciation. Use for "show a light when the alarm is active", "flash when alarm fires", "hide/show on alarm".
user_invocable: true
---

# Alarm-driven indicator (bind to an alarm's Active)

An alarm object exposes an `Active` (and `ActiveAndUnacked`, `NormalUnacked`, …)
Boolean. Bind an indicator's property to it — no NetLogic. Studio open, bridge
armed.

**Precondition:** the alarm already exists (e.g. `Alarms/<AlarmName>`). Creating
alarms is not yet a bridge op (roadmap tool B — non-UI object creation); use
Studio to add the alarm, then this skill wires the UI to it.

1. **Find the alarm's Active path** — browse `Alarms/<AlarmName>`; the state
   variable is `Alarms/<AlarmName>/Active` (or `ActiveAndUnacked`).
2. **Bind an indicator property** (mode `Read` — display only):
   - **Visibility:** `optix_bridge_bind_property(project, "UI/Screens/<S>/AlarmLight", "Visible", "Alarms/<AlarmName>/Active", mode="Read")`
   - **Blink:** `optix_bridge_bind_property(project, ".../AlarmLight", "Blink", "Alarms/<AlarmName>/Active", mode="Read")`
   - **Color** (1:1 to a color source): `bind_property(".../AlarmLight", "FillColor", "Model/AlarmColor", mode="Read")`.

## Fault=red / ok=green (conditional color)

A color that switches on the alarm Boolean — use `optix_bridge_attach_expression`
on the indicator's `FillColor`:
`expression="if({0}, 0xFFFF0000, 0xFF00FF00)", sources="Alarms/<AlarmName>/Active"`
(red when active, green otherwise). See the `optix-expression-converter` skill;
verify at runtime since converters no-op silently.

## Acknowledge / alarm commands

Wiring an **Acknowledge** button, or dropping a full Alarm Grid/Summary, needs the
generalized command-wire (roadmap D) / template-library instantiation (roadmap F).
This skill covers the read-only annunciation surface, which is bridge-ready today.

## Notes
- `optix_describe_node("Alarms/<AlarmName>")` to see the exact state-variable
  names on your alarm type.
- Severity banding for reference: 1-250 Low · 251-500 Medium · 501-750 High ·
  751-1000 Urgent.
