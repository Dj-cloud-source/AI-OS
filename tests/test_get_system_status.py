from typing import cast

import pytest

from ai_server.models.system_status import GetSystemStatusArguments, SystemStatus
from ai_server.tools.get_system_status import (
    GetSystemStatusTool,
    InvalidSystemStatusArgumentsError,
)


def test_mock_tool_returns_deterministic_payload_only_simulated_data() -> None:
    arguments = GetSystemStatusArguments()
    first = GetSystemStatusTool().invoke(arguments)
    second = GetSystemStatusTool().invoke(arguments)

    assert first == second
    assert type(first) is SystemStatus
    assert first.source == "mock"
    assert first.simulated is True
    assert first.target == "local-mock"
    assert first.hostname == "mock-server"
    assert first.cpu_percent == 12.5
    assert first.memory_percent == 34.0
    assert first.disk_percent == 45.5
    assert tuple(service.model_dump() for service in first.services) == (
        {"name": "mock-api", "state": "running"},
    )


def test_mock_tool_rejects_untrusted_arguments_with_explicit_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "SENSITIVE_TOOL_ARGUMENT_MARKER"
    arguments = GetSystemStatusArguments()

    def exploding_model_dump(*args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        raise RuntimeError(marker)

    monkeypatch.setattr(GetSystemStatusArguments, "model_dump", exploding_model_dump)
    tool = GetSystemStatusTool()

    with pytest.raises(
        InvalidSystemStatusArgumentsError,
        match="malformed arguments",
    ) as caught:
        tool.invoke(arguments)
    with pytest.raises(InvalidSystemStatusArgumentsError):
        tool.invoke(cast(GetSystemStatusArguments, object()))

    assert marker not in str(caught.value)
    assert caught.value.__cause__ is None
