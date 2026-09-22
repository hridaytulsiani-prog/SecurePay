from payments.models import AuditLog


def _json_safe(value):
    if value is None or isinstance(value, (str, int, float, bool, list, dict)):
        if isinstance(value, list):
            return [_json_safe(item) for item in value]
        if isinstance(value, dict):
            return {str(key): _json_safe(item) for key, item in value.items()}
        return value
    return str(value)


def _normalize_values(values):
    if not values:
        return {}
    return {key: _json_safe(value) for key, value in values.items()}


def _changed_fields(old_values, new_values):
    keys = set(old_values.keys()) | set(new_values.keys())
    return sorted(key for key in keys if old_values.get(key) != new_values.get(key))


def _ip_from_request(request):
    if not request:
        return None
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip() or None
    return request.META.get("REMOTE_ADDR")


def create_audit_log(
    *,
    case_type,
    case_id,
    action,
    actor_user=None,
    actor_display="",
    actor_role="",
    remarks="",
    old_values=None,
    new_values=None,
    metadata=None,
    source="",
    request=None,
):
    old_values = _normalize_values(old_values)
    new_values = _normalize_values(new_values)
    actor_display = actor_display or (actor_user.username if actor_user else "")

    return AuditLog.objects.create(
        case_type=case_type,
        case_id=str(case_id),
        action=action,
        actor_user=actor_user,
        actor_display=actor_display,
        actor_role=actor_role,
        remarks=remarks or "",
        old_values=old_values,
        new_values=new_values,
        changed_fields=_changed_fields(old_values, new_values),
        metadata=_json_safe(metadata or {}),
        source=source or "",
        request_method=request.method if request else "",
        request_path=request.path if request else "",
        ip_address=_ip_from_request(request),
    )
