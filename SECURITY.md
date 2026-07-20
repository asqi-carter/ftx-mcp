# Security & safety posture

`ftx-mcp` gives an LLM write access to a FactoryTalk Optix *project*.
This page is the 10-minute evaluator's map of what that access can and cannot
do, what gets recorded, and where the trust boundaries sit. Deep configuration
detail: [docs/security.md](docs/security.md).

## What the LLM can and cannot do

| | |
|---|---|
| **Can** | Author widgets/properties/bindings in the **open Studio project's in-memory model**, save it, run Studio's local emulator, screenshot/click the rendered canvas |
| **Cannot** | Deploy to hardware — this distribution contains **no deploy activation path** (the plumbing exists in source, with no runtime switch). Shipping is a human step in Studio's own Deploy dialog |
| **Cannot** | Edit project files on disk while Studio has them open (refused — Studio would overwrite them; live changes go through the bridge instead) |
| **Cannot** | Start or restart Studio. The operator owns the Studio process; the bridge is armed by a human right-click, per session |
| **Cannot** | Press "run" at a hardware target. Studio's F5 executes the *selected* deployment target, if a hardware target is the current selected, a password dialoge will popup and prevent deployment. This tool is only meant to be used with the emulator. |

## Write guardrails

Every tool declares MCP `readOnlyHint` / `destructiveHint` annotations
(contract-pinned by tests), so MCP hosts can auto-approve reads and gate
writes/destructive calls. Inside the write path, the bridge validates before
touching the model rather than failing after:

- **Undeclared properties are rejected** with the valid set
  (`unknown_property`) instead of being fabricated — fabricated orphans
  previously crashed Studio, so this gate is crash-safety, not pedantry.
- **Array-typed properties refuse scalar writes** (`unsupported_array_write`)
  — a scalar write to an array UA variable can terminate Studio.
- **Duplicate sibling names are refused loud** (`name_exists`) rather than
  creating path-unaddressable nodes.
- **Composite operations are transactional**: a failed step rolls back what
  was created (`rolled_back: true` in the response) instead of leaving
  orphans.
- **Structural refactors copy, never re-parent** live nodes (re-parenting
  corrupts the live model), and report link-fidelity honestly
  (`skipped`, `broken_links`).

## Traces

Every model-mutating operation appends a JSON line to a local audit trail —
`%LOCALAPPDATA%\ftx-mcp\logs\audit.jsonl`: timestamp, operation,
parameters, and outcome (`ok` / error) for all bridge writes, plus save,
emulator lifecycle, and CDP input events. It is a plain local file: inspect
it, ship it to your SIEM, or rotate it per site policy.

## Prompt-injection surface

- **Tool descriptions and bundled skills are static**, version-locked
  content from this repository. Nothing remote and nothing user-generated is
  ever interpolated into a tool description or skill.
- **Project content is data, not control.** Node names, YAML values, runtime
  log text, and OCR output read by tools do flow into the model's context —
  a hostile project file could carry adversarial text. The server never
  interprets or executes such content itself; mitigations for the model side
  are the annotation gating above, the absence of any deploy path, and the
  human ship step.
- **Network surface**: binds `127.0.0.1` by default; bearer-token auth is
  opt-in and enforced before any non-loopback bind. The service talks to
  nothing off the machine.

## Supported versions

| Version | Supported |
|---------|-----------|
| `v1.0.x` | yes (active) |

## Reporting a vulnerability

Open a GitHub security advisory (preferred) or an issue with the `security`
label. In scope: auth bypass on the HTTP/MCP surface; bearer-token disclosure
paths; path traversal or arbitrary write under `projects_root` /
`state_dir`; privilege escalation via `bootstrap/*.ps1`; subprocess injection
via Studio CLI argument construction. Out of scope (single-user-laptop threat
model): DPAPI blob recovery after full host compromise, and anything
requiring an already-elevated local attacker.
