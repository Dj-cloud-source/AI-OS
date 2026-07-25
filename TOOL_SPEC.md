

Tool:
    restart_service

Description:
    Restart a systemd service.

Risk:
    L2

Approval:
    Required

Rollback:
    Restart previous service state

Timeout:
    30s

Verification:
    systemctl status

Idempotent:
    true

Arguments:

    service_name

Returns:

    success

    stdout

    stderr

    verification

    duration
