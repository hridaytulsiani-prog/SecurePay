from copy import deepcopy


SPECIAL_ACCESS_DEFINITIONS = [
    {
        "key": "manage_enquiry_notes",
        "label": "Manage Enquiry Notes",
        "description": "Add, edit, and delete enquiry notes.",
        "permission": "enquiries.manage",
        "has_hourly_limit": False,
    },
    {
        "key": "update_refund_release_decision",
        "label": "Update Refund / Release Decision",
        "description": "Record Money Refunded or Money To Merchant decisions.",
        "permission": "enquiries.manage",
        "has_hourly_limit": True,
        "default_hourly_limit": 10,
    },
    {
        "key": "refund_money",
        "label": "Refund Money",
        "description": "Permission bucket for future real refund approval/release actions.",
        "permission": "money.refund",
        "has_hourly_limit": True,
        "default_hourly_limit": 10,
    },
    {
        "key": "release_money",
        "label": "Release Money",
        "description": "Permission bucket for future merchant payout/release approval actions.",
        "permission": "money.release",
        "has_hourly_limit": True,
        "default_hourly_limit": 10,
    },
    {
        "key": "courier_verification_decision",
        "label": "Courier Verification Decision",
        "description": "Mark shipment/courier verification as verified or not verified.",
        "permission": "courier.decide",
        "has_hourly_limit": True,
        "default_hourly_limit": 20,
    },
    {
        "key": "pa_control",
        "label": "PA Control",
        "description": "Use payment aggregator control-plane actions.",
        "permission": "pa_control.manage",
        "has_hourly_limit": True,
        "default_hourly_limit": 10,
    },
    {
        "key": "audit_trail",
        "label": "Audit Trail",
        "description": "View audit logs.",
        "permission": "audit.view",
        "has_hourly_limit": False,
    },
]


def default_special_access():
    rules = {}
    for item in SPECIAL_ACCESS_DEFINITIONS:
        rules[item["key"]] = {
            "enabled": False,
            "hourly_limit": item.get("default_hourly_limit") if item.get("has_hourly_limit") else None,
        }
    return rules


def normalize_special_access(raw_rules):
    rules = default_special_access()
    raw_rules = raw_rules or {}
    for item in SPECIAL_ACCESS_DEFINITIONS:
        key = item["key"]
        incoming = raw_rules.get(key) if isinstance(raw_rules.get(key), dict) else {}
        rules[key]["enabled"] = bool(incoming.get("enabled", rules[key]["enabled"]))
        if item.get("has_hourly_limit"):
            try:
                limit = int(incoming.get("hourly_limit", rules[key]["hourly_limit"]))
            except (TypeError, ValueError):
                limit = item.get("default_hourly_limit", 10)
            rules[key]["hourly_limit"] = max(0, limit)
    return rules


def serialize_special_access(raw_rules):
    rules = normalize_special_access(raw_rules)
    output = []
    for item in SPECIAL_ACCESS_DEFINITIONS:
        row = deepcopy(item)
        row.update(rules[item["key"]])
        output.append(row)
    return output
