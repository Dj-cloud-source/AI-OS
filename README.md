# AI Server Runtime

A local-first, policy-controlled Runtime for safe and testable server
operations.

Phase 0 provides an installable Python foundation, a deterministic Mock Tool,
and a minimal Runtime lifecycle without connecting to a real server.

## Current capabilities

- Python 3.12+ package using the `src` layout
- `ai-server version`
- `ai-server doctor`
- Typed and immutable Runtime models
- Deterministic L0 `get_system_status` Mock Tool
- Explicit Planner, Policy, Executor, and Verifier boundaries
- Structured execution audit events

Phase 0 contains no SSH, Shell, Docker, Kubernetes, database writes, LLM
connections, configuration changes, or destructive operations.

## Local setup

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/ai-server version
.venv/bin/ai-server doctor
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
