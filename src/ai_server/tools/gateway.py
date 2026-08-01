"""Deterministic validation and result-envelope boundary for registered Tools."""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from time import monotonic_ns
from types import MappingProxyType
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


class GatewayDispatchStatus(StrEnum):
    """State whether the registered Tool handler has been entered."""

    NOT_DISPATCHED = "not_dispatched"
    HANDLER_DISPATCHED = "handler_dispatched"


@dataclass(frozen=True, slots=True)
class GatewayDispatchReceipt:
    """Bind one trusted Tool result to authoritative dispatch-side facts."""

    result: ToolResult[BaseModel]
    dispatch_status: GatewayDispatchStatus
    mutates_remote_state: bool


class ToolGatewayError(Exception):
    """Base class for failures without a trustworthy ToolCall envelope."""

    code: ClassVar[str] = "tool_gateway_error"
    dispatch_status: GatewayDispatchStatus = GatewayDispatchStatus.NOT_DISPATCHED
    mutates_remote_state: bool | None = None


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
    """Raised when immutable Tool identity or result integrity cannot be trusted."""

    code: ClassVar[str] = "tool_integrity"


class PostDispatchToolIntegrityError(ToolIntegrityError):
    """Report an unsafe result-envelope failure after handler dispatch."""

    code: ClassVar[str] = "post_dispatch_tool_integrity"
    dispatch_status: GatewayDispatchStatus = GatewayDispatchStatus.HANDLER_DISPATCHED

    def __init__(self, message: str, *, mutates_remote_state: bool) -> None:
        """Record authoritative side-effect metadata without leaking failure data."""
        super().__init__(message)
        self.mutates_remote_state = mutates_remote_state


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
        return self._invoke_with_receipt(call).result

    def validate_result[ArgumentsT: BaseModel, PayloadT: BaseModel](
        self,
        call: ToolCall[ArgumentsT],
        result: ToolResult[PayloadT],
    ) -> bool:
        """Validate retained result evidence against one exact call without dispatch."""
        try:
            trusted_call = _validate_call(call)
            registered = self._registry._resolve(
                trusted_call.tool_id,
                trusted_call.tool_version,
            )
            if (
                trusted_call.contract_hash != registered.metadata.contract_hash
                or trusted_call.implementation_hash != registered.metadata.implementation_hash
            ):
                return False
            arguments = _validate_arguments(trusted_call, registered)
            if (
                arguments is None
                or not _target_matches_contract(
                    trusted_call.target,
                    arguments,
                    registered,
                )
                or canonical_json_sha256(arguments) != trusted_call.arguments_hash
            ):
                return False
            validated_result = _validate_retained_result(result, registered)
            if validated_result is None or not _result_identity_matches_call(
                validated_result,
                trusted_call,
            ):
                return False
            result_document = cast(
                dict[str, JsonValue],
                validated_result.model_dump(mode="json", warnings="error"),
            )
            if (
                validated_result.duration_ms > registered.metadata.timeout_ms
                or _contains_forbidden_content(result_document)
                or not result_matches_contract(result_document, registered.contract)
            ):
                return False
            if validated_result.success:
                payload = _validate_payload(validated_result.data, registered)
                expected_evidence = None if payload is None else _safe_evidence(payload, registered)
                return (
                    expected_evidence is not None and validated_result.evidence == expected_evidence
                )
            return _failure_matches_contract(validated_result, registered)
        except BaseException:
            return False

    def _invoke_with_receipt[ArgumentsT: BaseModel](
        self,
        call: ToolCall[ArgumentsT],
    ) -> GatewayDispatchReceipt:
        """Invoke once and retain internal handler-dispatch and side-effect facts."""
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
            return _pre_dispatch_failure_receipt(
                trusted_call,
                registered,
                code="invalid_arguments",
            )
        if not _target_matches_contract(
            trusted_call.target,
            arguments,
            registered,
        ):
            return _pre_dispatch_failure_receipt(
                trusted_call,
                registered,
                code="target_not_allowed",
            )

        try:
            computed_arguments_hash = canonical_json_sha256(arguments)
        except (CanonicalizationError, TypeError, ValueError):
            return _pre_dispatch_failure_receipt(
                trusted_call,
                registered,
                code="invalid_arguments",
            )
        except BaseException:
            return _pre_dispatch_failure_receipt(
                trusted_call,
                registered,
                code="invalid_arguments",
            )
        if computed_arguments_hash != trusted_call.arguments_hash:
            return _pre_dispatch_failure_receipt(
                trusted_call,
                registered,
                code="arguments_hash_mismatch",
            )

        start_ns = _read_clock(self._clock)
        if start_ns is None:
            return _pre_dispatch_failure_receipt(
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
            return _post_dispatch_failure_receipt(
                trusted_call,
                registered,
                code="gateway_clock_failed",
            )

        elapsed_ns = end_ns - start_ns
        duration_ms = _duration_ms(elapsed_ns)
        if elapsed_ns > metadata.timeout_ms * 1_000_000:
            return _post_dispatch_failure_receipt(
                trusted_call,
                registered,
                code="tool_timeout",
                duration_ms=metadata.timeout_ms,
            )
        if execution_failed:
            return _post_dispatch_failure_receipt(
                trusted_call,
                registered,
                code="tool_execution_failed",
                duration_ms=duration_ms,
            )

        validated_payload = _validate_payload(payload, registered)
        if validated_payload is None:
            return _post_dispatch_failure_receipt(
                trusted_call,
                registered,
                code="malformed_tool_output",
                duration_ms=duration_ms,
            )
        evidence = _safe_evidence(validated_payload, registered)
        if evidence is None:
            return _post_dispatch_failure_receipt(
                trusted_call,
                registered,
                code="result_redaction_failed",
                duration_ms=duration_ms,
            )

        try:
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
            result_is_valid = result_matches_contract(
                cast(dict[str, JsonValue], result_document),
                registered.contract,
            )
        except BaseException:
            result_is_valid = False
        if not result_is_valid:
            return _post_dispatch_failure_receipt(
                trusted_call,
                registered,
                code="malformed_tool_output",
                duration_ms=duration_ms,
            )
        return _receipt(
            result,
            registered,
            dispatch_status=GatewayDispatchStatus.HANDLER_DISPATCHED,
        )


def _validate_call[ArgumentsT: BaseModel](
    call: ToolCall[ArgumentsT],
) -> ToolCall[BaseModel]:
    if not isinstance(call, ToolCall):
        raise InvalidToolCallError("Tool Gateway received a malformed ToolCall")
    try:
        call_model = type(call)
        generic_metadata = getattr(call_model, "__pydantic_generic_metadata__", None)
        if type(generic_metadata) is not dict:
            raise TypeError
        arguments_types = generic_metadata.get("args")
        if (
            generic_metadata.get("origin") is not ToolCall
            or type(arguments_types) is not tuple
            or len(arguments_types) != 1
            or not isinstance(arguments_types[0], type)
            or not issubclass(arguments_types[0], BaseModel)
            or generic_metadata.get("parameters") != ()
        ):
            raise TypeError
        model_factory = getattr(ToolCall, "__class_getitem__", None)
        if not callable(model_factory):
            raise TypeError
        canonical_model = cast(
            type[BaseModel],
            model_factory(arguments_types[0]),
        )
        if call_model is not canonical_model:
            raise TypeError
        validated = canonical_model.model_validate(
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


def _validate_retained_result[PayloadT: BaseModel](
    result: ToolResult[PayloadT],
    registered: _RegisteredTool,
) -> ToolResult[BaseModel] | None:
    try:
        result_model = type(result)
        generic_metadata = getattr(result_model, "__pydantic_generic_metadata__", None)
        if type(generic_metadata) is not dict:
            return None
        payload_types = generic_metadata.get("args")
        if (
            generic_metadata.get("origin") is not ToolResult
            or type(payload_types) is not tuple
            or len(payload_types) != 1
            or payload_types[0] not in {BaseModel, registered.output_model}
            or generic_metadata.get("parameters") != ()
        ):
            return None
        model_factory = getattr(ToolResult, "__class_getitem__", None)
        if not callable(model_factory):
            return None
        canonical_input_model = cast(type[BaseModel], model_factory(payload_types[0]))
        if result_model is not canonical_input_model:
            return None
        if (
            type(result.target) is not TargetReference
            or not _is_frozen_json_object(result.evidence)
            or (result.error is not None and type(result.error) is not ToolError)
            or (result.data is not None and type(result.data) is not registered.output_model)
        ):
            return None
        canonical_output_model = cast(
            type[BaseModel],
            model_factory(registered.output_model),
        )
        validated = canonical_output_model.model_validate(
            result.model_dump(mode="python", warnings="error"),
            strict=True,
        )
        return cast(ToolResult[BaseModel], validated)
    except BaseException:
        return None


def _is_frozen_json_object(value: object) -> bool:
    if type(value) is not MappingProxyType:
        return False
    mapping = cast(dict[object, object], value)
    return all(
        type(key) is str and _is_frozen_json_value(nested) for key, nested in mapping.items()
    )


def _is_frozen_json_value(value: object) -> bool:
    if type(value) is MappingProxyType:
        return _is_frozen_json_object(value)
    if type(value) is tuple:
        return all(_is_frozen_json_value(nested) for nested in cast(tuple[object, ...], value))
    return value is None or type(value) in {bool, int, float, str}


def _result_identity_matches_call(
    result: ToolResult[BaseModel],
    call: ToolCall[BaseModel],
) -> bool:
    return (
        result.invocation_id == call.invocation_id
        and result.plan_step_id == call.plan_step_id
        and result.tool_id == call.tool_id
        and result.tool_version == call.tool_version
        and result.contract_hash == call.contract_hash
        and result.arguments_hash == call.arguments_hash
        and result.target == call.target
    )


def _failure_matches_contract(
    result: ToolResult[BaseModel],
    registered: _RegisteredTool,
) -> bool:
    if result.data is not None or result.evidence != {} or type(result.error) is not ToolError:
        return False
    definition = next(
        (
            candidate
            for candidate in registered.contract.errors
            if candidate.code == result.error.code
        ),
        None,
    )
    return (
        definition is not None
        and result.error.category is definition.category
        and result.error.message == definition.message
        and result.error.retryable is definition.retryable
    )


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


def _receipt(
    result: ToolResult[BaseModel],
    registered: _RegisteredTool,
    *,
    dispatch_status: GatewayDispatchStatus,
) -> GatewayDispatchReceipt:
    return GatewayDispatchReceipt(
        result=result,
        dispatch_status=dispatch_status,
        mutates_remote_state=registered.metadata.side_effects.mutates_remote_state,
    )


def _pre_dispatch_failure_receipt(
    call: ToolCall[BaseModel],
    registered: _RegisteredTool,
    *,
    code: str,
    duration_ms: int = 0,
) -> GatewayDispatchReceipt:
    try:
        result = _failure_result(
            call,
            registered,
            code=code,
            duration_ms=duration_ms,
        )
    except ToolGatewayError:
        raise
    except BaseException:
        raise ToolIntegrityError(
            "Registered Tool cannot represent a structured failure safely"
        ) from None
    return _receipt(
        result,
        registered,
        dispatch_status=GatewayDispatchStatus.NOT_DISPATCHED,
    )


def _post_dispatch_failure_receipt(
    call: ToolCall[BaseModel],
    registered: _RegisteredTool,
    *,
    code: str,
    duration_ms: int = 0,
) -> GatewayDispatchReceipt:
    mutates_remote_state = registered.metadata.side_effects.mutates_remote_state
    try:
        result = _failure_result(
            call,
            registered,
            code=code,
            duration_ms=duration_ms,
        )
    except BaseException:
        raise PostDispatchToolIntegrityError(
            "Post-dispatch Tool result cannot represent a structured failure safely",
            mutates_remote_state=mutates_remote_state,
        ) from None
    return _receipt(
        result,
        registered,
        dispatch_status=GatewayDispatchStatus.HANDLER_DISPATCHED,
    )


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
    "GatewayDispatchReceipt",
    "GatewayDispatchStatus",
    "InvalidGatewayConfigurationError",
    "InvalidToolCallError",
    "PostDispatchToolIntegrityError",
    "ToolIntegrityError",
    "ToolGateway",
    "ToolGatewayError",
    "ToolResolutionError",
]
