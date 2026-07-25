from typing import cast

import pytest
from pydantic import BaseModel

from ai_server.models.system_status import GetSystemStatusArguments, SystemStatus
from ai_server.models.tool import RiskLevel, ToolResult
from ai_server.runtime.errors import ToolExecutionError
from ai_server.tools.get_system_status import GET_SYSTEM_STATUS_METADATA, get_system_status


def test_mock_tool_metadata_is_static_l0() -> None:
    assert GET_SYSTEM_STATUS_METADATA.name == "get_system_status"
    assert GET_SYSTEM_STATUS_METADATA.version == "1.0.0"
    assert GET_SYSTEM_STATUS_METADATA.risk_level is RiskLevel.L0
    assert GET_SYSTEM_STATUS_METADATA.idempotent is True
    assert GET_SYSTEM_STATUS_METADATA.timeout_seconds == 1.0
    assert GET_SYSTEM_STATUS_METADATA.input_model == "GetSystemStatusArguments"
    assert GET_SYSTEM_STATUS_METADATA.output_model == "SystemStatus"


def test_mock_tool_returns_deterministic_typed_simulated_data() -> None:
    arguments = GetSystemStatusArguments()
    first = get_system_status(arguments)
    second = get_system_status(arguments)

    assert first == second
    assert isinstance(first, ToolResult)
    assert isinstance(first.data, BaseModel)
    assert isinstance(first.data, SystemStatus)
    assert first.success is True
    assert first.duration_ms == 0
    assert first.data.source == "mock"
    assert first.data.simulated is True
    assert first.data.target == "local-mock"
    assert first.data.hostname == "mock-server"
    assert first.data.cpu_percent == 12.5
    assert first.data.memory_percent == 34.0
    assert first.data.disk_percent == 45.5
    assert tuple(service.model_dump() for service in first.data.services) == (
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

    with pytest.raises(ToolExecutionError, match="malformed arguments") as caught:
        get_system_status(arguments)
    with pytest.raises(ToolExecutionError):
        get_system_status(cast(GetSystemStatusArguments, object()))

    assert marker not in str(caught.value)
    assert caught.value.__cause__ is None
