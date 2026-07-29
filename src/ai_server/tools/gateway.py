"""Deterministic validation and result-envelope boundary for registered Tools."""

from collections.abc import Callable
from time import monotonic_ns
from typing import ClassVar, cast

from jsonschema import Draft202012Validator, FormatChecker
from pydantic import BaseModel, JsonValue

from ai_server.models.tool import (
    TargetReference,
    ToolCall,
    ToolError,
    ToolResult,
)
from ai_server.tools.artifact_loader import result_matches_contract
from ai_server.tools.hashing import (
    CanonicalizationError,
    canonical_json_bytes,
    canonical_json_sha256,
)
from ai_server.tools.protocol import FORBIDDEN_DATA_KEYS, FORBIDDEN_VALUE_MARKERS
from ai_server.tools.registry import (
    ToolRegistry,
    UnknownToolError,
    _RegisteredTool,
)

Clock = Callable[[], int]


class ToolGatewayError(Exception):
    """Base class for failures without a trustworthy ToolCall envelope."""

    code: ClassVar[str] = "tool_gateway_error"


class InvalidToolCallError(ToolGatewayError):
    """Raised when an input cannot be trusted as a strict ToolCall."""

    code: ClassVar[str] = "invalid_tool_call"


class InvalidGatewayConfigurationError(ToolGatewayError):
    """Raised when the Gateway is constructed with unsafe dependencies."""

    code: ClassVar[str] = "invalid_gateway_configuration"


class ToolResolutionError(ToolGatewayError):
    """Raised before invocation when an exact registered Tool cannot be resolved."""

    code: ClassVar[str] = "tool_resolution"


class ToolIntegrityError(ToolGatewayError):
    """Raised before invocation when immutable Tool hashes do not match."""

    code: ClassVar[str] = "tool_integrity"


class ToolGateway:
    """Validate exact calls and invoke payload handlers through a frozen Registry."""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        clock: Clock = monotonic_ns,
    ) -> None:
        """Bind one frozen Registry and a monotonic nanosecond clock."""
        if type(registry) is not ToolRegistry or not registry.is_frozen:
            raise InvalidGatewayConfigurationError(
                "Tool Gateway requires an exact frozen Tool Registry"
            )
        if not callable(clock):
            raise InvalidGatewayConfigurationError(
                "Tool Gateway requires a callable monotonic clock"
            )
        self._registry = registry
        self._clock = clock

    def invoke[ArgumentsT: BaseModel](
        self,
        call: ToolCall[ArgumentsT],
    ) -> ToolResult[BaseModel]:
        """Validate, dispatch once, and return a trusted structured result."""
        trusted_call = _validate_call(call)

        try:
            registered = self._registry._resolve(
                trusted_call.tool_id,
                trusted_call.tool_version,
            )
        except UnknownToolError:
            raise ToolResolutionError("Requested Tool identity is not registered") from None
        except BaseException:
            raise ToolResolutionError(
                "Tool Registry could not resolve the request safely"
            ) from None

        metadata = registered.metadata
        if trusted_call.contract_hash != metadata.contract_hash:
            raise ToolIntegrityError("Tool contract integrity validation failed")
        if trusted_call.implementation_hash != metadata.implementation_hash:
            raise ToolIntegrityError("Tool implementation integrity validation failed")

        arguments = _validate_arguments(trusted_call, registered)
        if arguments is None:
            return _failure_result(
                trusted_call,
                registered,
                code="invalid_arguments",
            )
        if not _target_matches_contract(
            trusted_call.target,
            arguments,
            registered,
        ):
            return _failure_result(
                trusted_call,
                registered,
                code="target_not_allowed",
            )

        try:
            computed_arguments_hash = canonical_json_sha256(arguments)
        except (CanonicalizationError, TypeError, ValueError):
            return _failure_result(
                trusted_call,
                registered,
                code="invalid_arguments",
            )
        except BaseException:
            return _failure_result(
                trusted_call,
                registered,
                code="invalid_arguments",
            )
        if computed_arguments_hash != trusted_call.arguments_hash:
            return _failure_result(
                trusted_call,
                registered,
                code="arguments_hash_mismatch",
            )

        start_ns = _read_clock(self._clock)
        if start_ns is None:
            return _failure_result(
                trusted_call,
                registered,
                code="gateway_clock_failed",
            )

        payload: BaseModel | None = None
        execution_failed = False
        try:
            payload = registered._invoke_payload(arguments)
        except BaseException:
            execution_failed = True

        end_ns = _read_clock(self._clock)
        if end_ns is None or end_ns < start_ns:
            return _failure_result(
                trusted_call,
                registered,
                code="gateway_clock_failed",
            )

        elapsed_ns = end_ns - start_ns
        duration_ms = _duration_ms(elapsed_ns)
        if elapsed_ns > metadata.timeout_ms * 1_000_000:
            return _failure_result(
                trusted_call,
                registered,
                code="tool_timeout",
                duration_ms=metadata.timeout_ms,
            )
        if execution_failed:
            return _failure_result(
                trusted_call,
                registered,
                code="tool_execution_failed",
                duration_ms=duration_ms,
            )

        validated_payload = _validate_payload(payload, registered)
        if validated_payload is None:
            return _failure_result(
                trusted_call,
                registered,
                code="malformed_tool_output",
                duration_ms=duration_ms,
            )
        evidence = _safe_evidence(validated_payload, registered)
        if evidence is None:
            return _failure_result(
                trusted_call,
                registered,
                code="result_redaction_failed",
                duration_ms=duration_ms,
            )

        result = ToolResult[BaseModel](
            invocation_id=trusted_call.invocation_id,
            plan_step_id=trusted_call.plan_step_id,
            tool_id=trusted_call.tool_id,
            tool_version=trusted_call.tool_version,
            contract_hash=trusted_call.contract_hash,
            arguments_hash=trusted_call.arguments_hash,
            target=trusted_call.target,
            success=True,
            data=validated_payload,
            evidence=evidence,
            error=None,
            duration_ms=duration_ms,
        )
        result_document = result.model_dump(mode="json", warnings="error")
        if not result_matches_contract(
            cast(dict[str, JsonValue], result_document),
            registered.contract,
        ):
            return _failure_result(
                trusted_call,
                registered,
                code="malformed_tool_output",
                duration_ms=duration_ms,
            )
        return result


def _validate_call[ArgumentsT: BaseModel](
    call: ToolCall[ArgumentsT],
) -> ToolCall[BaseModel]:
    if not isinstance(call, ToolCall):
        raise InvalidToolCallError("Tool Gateway received a malformed ToolCall")
    try:
        call_model = type(call)
        validated = call_model.model_validate(
            call.model_dump(mode="python", warnings="error"),
            strict=True,
        )
        return cast(ToolCall[BaseModel], validated)
    except BaseException:
        raise InvalidToolCallError("Tool Gateway received a malformed ToolCall") from None


def _validate_arguments(
    call: ToolCall[BaseModel],
    registered: _RegisteredTool,
) -> BaseModel | None:
    input_model = registered.input_model
    if type(call.arguments) is not input_model:
        return None
    try:
        validated = input_model.model_validate(
            call.arguments.model_dump(mode="python", warnings="error"),
            strict=True,
        )
        input_document = validated.model_dump(mode="json", warnings="error")
        validator = Draft202012Validator(
            registered.contract.input_schema,
            format_checker=FormatChecker(),
        )
        if next(validator.iter_errors(input_document), None) is not None:
            return None
        return validated
    except BaseException:
        return None


def _validate_payload(
    payload: BaseModel | None,
    registered: _RegisteredTool,
) -> BaseModel | None:
    output_model = registered.output_model
    if type(payload) is not output_model:
        return None
    try:
        validated = output_model.model_validate(
            payload.model_dump(mode="python", warnings="error"),
            strict=True,
        )
        return validated
    except BaseException:
        return None


def _target_matches_contract(
    target: TargetReference,
    arguments: BaseModel,
    registered: _RegisteredTool,
) -> bool:
    scope = registered.contract.target_scope
    if target.resource_type != scope.resource_type or scope.maximum_targets != 1:
        return False
    try:
        argument_document = arguments.model_dump(mode="json", warnings="error")
        selector = argument_document.get(scope.selector_field)
    except BaseException:
        return False
    return type(selector) is str and selector == target.target_id and selector == target.resource_id


def _safe_evidence(
    payload: BaseModel,
    registered: _RegisteredTool,
) -> dict[str, JsonValue] | None:
    try:
        document = payload.model_dump(mode="json", warnings="error")
        redaction = registered.contract.redaction
        if any(field in document for field in redaction.output_fields):
            return None
        if _contains_forbidden_content(document):
            return None
        if len(canonical_json_bytes(cast(JsonValue, document))) > (
            redaction.max_retained_payload_bytes
        ):
            return None
        return {
            field: cast(JsonValue, document[field])
            for field in redaction.safe_evidence_fields
            if field in document
        }
    except BaseException:
        return None


def _contains_forbidden_content(value: object) -> bool:
    if type(value) is dict:
        mapping = cast(dict[object, object], value)
        return any(
            (type(key) is str and key.lower() in FORBIDDEN_DATA_KEYS)
            or _contains_forbidden_content(item)
            for key, item in mapping.items()
        )
    if type(value) is list:
        return any(_contains_forbidden_content(item) for item in cast(list[object], value))
    if type(value) is str:
        normalized = value.lower()
        return any(marker in normalized for marker in FORBIDDEN_VALUE_MARKERS)
    return False


def _read_clock(clock: Clock) -> int | None:
    try:
        value = clock()
    except BaseException:
        return None
    if type(value) is not int or value < 0:
        return None
    return value


def _duration_ms(elapsed_ns: int) -> int:
    if elapsed_ns == 0:
        return 0
    return (elapsed_ns + 999_999) // 1_000_000


def _failure_result(
    call: ToolCall[BaseModel],
    registered: _RegisteredTool,
    *,
    code: str,
    duration_ms: int = 0,
) -> ToolResult[BaseModel]:
    definition = next(
        (candidate for candidate in registered.contract.errors if candidate.code == code),
        None,
    )
    if definition is None:
        raise ToolIntegrityError("Registered Tool omits a required structured failure")
    result = ToolResult[BaseModel](
        invocation_id=call.invocation_id,
        plan_step_id=call.plan_step_id,
        tool_id=call.tool_id,
        tool_version=call.tool_version,
        contract_hash=call.contract_hash,
        arguments_hash=call.arguments_hash,
        target=call.target,
        success=False,
        data=None,
        error=ToolError(
            code=definition.code,
            category=definition.category,
            message=definition.message,
            retryable=definition.retryable,
        ),
        duration_ms=duration_ms,
    )
    result_document = result.model_dump(mode="json", warnings="error")
    if not result_matches_contract(
        cast(dict[str, JsonValue], result_document),
        registered.contract,
    ):
        raise ToolIntegrityError("Registered Tool cannot represent a structured failure safely")
    return result


__all__ = [
    "Clock",
    "InvalidGatewayConfigurationError",
    "InvalidToolCallError",
    "ToolIntegrityError",
    "ToolGateway",
    "ToolGatewayError",
    "ToolResolutionError",
]
