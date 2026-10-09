from payments.models import DecisionHistory


def _json_safe(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    return str(value)


def add_decision_history(
    *,
    case_type,
    case_id,
    order_id="",
    status,
    title,
    remarks="",
    actor_user=None,
    actor_display="",
    actor_role="",
    metadata=None,
):
    case_id = str(case_id)
    latest = (
        DecisionHistory.objects.filter(case_type=case_type, case_id=case_id)
        .order_by("-version", "-id")
        .first()
    )
    actor_display = actor_display or (actor_user.username if actor_user else "")

    return DecisionHistory.objects.create(
        case_type=case_type,
        case_id=case_id,
        order_id=str(order_id or ""),
        status=status,
        title=title,
        remarks=remarks or "",
        actor_user=actor_user,
        actor_display=actor_display,
        actor_role=actor_role,
        version=(latest.version + 1) if latest else 1,
        metadata=_json_safe(metadata or {}),
    )
