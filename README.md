# AI Server Runtime

A local-first, policy-controlled Runtime for safe and testable server
operations.

Phases 0–4 provide an installable Python foundation, a fail-closed Runtime
lifecycle, an artifact-driven Tool Protocol, and a reviewed deterministic
Policy and process-local Approval boundary without connecting to a real server.
Human-approved work remains paused because Phase 5 has not connected
authorization consumption to Executor dispatch. The only registered and
authorized Tool is the deterministic local `get_system_status@1.0.0` Mock.

## Current capabilities

- Python 3.12+ package using the `src` layout
- `ai-server version`
- `ai-server doctor`
- `ai-server run get_system_status` for the existing L0 Mock lifecycle
- Typed and immutable Runtime models
- Five package-resident Tool Protocol JSON Schemas
- Artifact-validated, immutable-after-startup Tool Registry
- Exact-version, hash-bound Tool Gateway dispatch through Executor
- Versioned strict-JSON Policy Profile with an independently hash-bound local
  review record
- Structured per-Step and aggregate Policy decisions with exact capability
  matching and an L0–L3 fail-closed matrix
- Reviewed Policy Profile v1.1 TTL ceilings for Review, Plan Approval, and L3
  confirmation
- Immutable exact-plan Approval snapshots, canonical hashes, process-local
  Review/Commit records, audit events, expiration, invalidation, and one-time
  consumption protocols
- Interactive-TTY-only `COMMIT <full-plan-hash>` / `REJECT` handling; a Commit
  records authorization but does not resume or dispatch human-approved work
- Deterministic L0 `get_system_status@1.0.0` payload-only Mock Tool
- Explicit Planner, Policy, Executor, and Verifier boundaries
- Immutable structured outcomes and ordered lifecycle events
- Approval pause, exact Review/Commit, and human rejection without pre-Phase-5
  resume
- Structured lifecycle and execution audit events
- Repository-local `uv.lock` plus source, wheel, and clean-install build gates

The current implementation contains no SSH, Shell, Docker, Kubernetes,
database writes, network or real-server access, LLM connections, configuration
changes, or destructive operations.

## Local setup

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/ai-server version
.venv/bin/ai-server doctor
.venv/bin/ai-server run get_system_status
```

## Quality checks

```bash
.venv/bin/python -m pytest
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src tests
```

## Project documents

- [Development rules](AGENTS.md)
- [Roadmap](ROADMAP.md)
- [Vision](docs/VISION.md)
- [Philosophy](docs/PHILOSOPHY.md)
- [Architecture](docs/ARCHITECTURE.md)
- [State machine](docs/STATE_MACHINE.md)
- [Tool specification](docs/TOOL_SPEC.md)
- [Implementation plan](docs/IMPLEMENTATION_PLAN.md)

## License

See [LICENSE](LICENSE).
