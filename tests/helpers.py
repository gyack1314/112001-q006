"""测试共用报文构造。"""


def v1_asset(
    asset="A-1",
    standard="LTE",
    capacity=10,
    unit="Mbps",
    valid_from="2026-01-01",
    valid_to=None,
    schema_version="1",
    **extra,
):
    payload = {
        "msg_type": "asset_report",
        "schema_version": schema_version,
        "report_id": f"rpt-{asset}-{valid_from}",
        "reported_at": "2026-01-01T00:00:00Z",
        "valid_from": valid_from,
        "valid_to": valid_to,
        "vendor": "vendor-t",
        "asset": asset,
        "standard": standard,
        "capacity": capacity,
        "unit": unit,
    }
    payload.update(extra)
    return payload


def v2_asset(
    asset="A-2",
    standard="5G",
    down=15,
    up=5,
    unit="Gbps",
    valid_from="2026-03-01",
    valid_to=None,
    shared_resource=None,
    schema_version="2",
    **extra,
):
    payload = {
        "msg_type": "asset_report",
        "schema_version": schema_version,
        "report_id": f"rpt-{asset}-{valid_from}",
        "reported_at": "2026-03-01T00:00:00Z",
        "valid_from": valid_from,
        "valid_to": valid_to,
        "vendor": "vendor-t",
        "asset": asset,
        "standard": standard,
        "capacity": {"down": down, "up": up},
        "unit": unit,
    }
    if shared_resource is not None:
        payload["shared_resource"] = shared_resource
    payload.update(extra)
    return payload


def v2_resource(
    resource_id="R-1",
    scope_type="site",
    scope_id="SITE-1",
    standard="5G",
    down=40,
    up=10,
    unit="Gbps",
    valid_from="2026-03-01",
    valid_to=None,
    **extra,
):
    payload = {
        "msg_type": "shared_resource_report",
        "schema_version": "2",
        "report_id": f"rpt-{resource_id}-{valid_from}",
        "reported_at": "2026-03-01T00:00:00Z",
        "valid_from": valid_from,
        "valid_to": valid_to,
        "resource_id": resource_id,
        "owner_scope": {"scope_type": scope_type, "scope_id": scope_id},
        "capacity": {"down": down, "up": up},
        "unit": unit,
    }
    if standard is not None:
        payload["standard"] = standard
    payload.update(extra)
    return payload
