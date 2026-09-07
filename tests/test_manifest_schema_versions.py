from __future__ import annotations

import json
from pathlib import Path

import pytest

jsonschema = pytest.importorskip("jsonschema")


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "schemas" / "manifest-v2.schema.json").read_text(encoding="utf-8"))


def manifest(revision: str, auth: dict[str, str]) -> dict:
    return {
        "protocol": "zhuojian-subsystem",
        "version": 2,
        "contractRevision": revision,
        "enterprise": {"key": "alphabet", "name": "Alphabet"},
        "applicationSlug": "orders",
        "applicationName": "订单系统",
        "eventsUrl": "/api/integration/events",
        "eventDeliveriesUrl": "/api/integration/event-deliveries",
        "auth": auth,
        "modules": [
            {
                "moduleKey": "orders",
                "name": "订单",
                "route": "/orders",
                "departments": [{"key": "sales", "name": "销售部", "role": "owner"}],
                "accessRoles": [
                    {
                        "roleKey": "viewer",
                        "name": "查看者",
                        "pageKeys": ["orders.list"],
                        "actionKeys": [],
                    }
                ],
                "pages": [
                    {
                        "pageKey": "orders.list",
                        "name": "订单列表",
                        "routePattern": "/orders",
                        "actionKeys": [],
                        "contextSchema": {},
                    }
                ],
                "actions": [],
            }
        ],
    }


def validate(payload: dict) -> None:
    jsonschema.Draft202012Validator(SCHEMA).validate(payload)


def test_schema_accepts_v24_legacy_auth_shape():
    validate(manifest("2.4", {"ssoPath": "/api/integration/sso", "algorithm": "HS256"}))


def test_schema_accepts_v25_authorization_code_shape():
    validate(
        manifest(
            "2.5",
            {"ssoPath": "/api/integration/sso", "mode": "authorization_code"},
        )
    )


@pytest.mark.parametrize(("location", "field"), [("root", "teams"), ("module", "teamId")])
def test_schema_rejects_retired_team_authorization_metadata(location: str, field: str):
    payload = manifest("2.5", {"ssoPath": "/api/integration/sso", "mode": "authorization_code"})
    target = payload if location == "root" else payload["modules"][0]
    target[field] = [] if field == "teams" else "legacy-team"
    with pytest.raises(jsonschema.ValidationError):
        validate(payload)


@pytest.mark.parametrize(
    ("revision", "auth"),
    [
        ("2.4", {"ssoPath": "/api/integration/sso", "mode": "authorization_code"}),
        ("2.5", {"ssoPath": "/api/integration/sso", "algorithm": "HS256"}),
        ("2.6", {"ssoPath": "/api/integration/sso", "mode": "authorization_code"}),
    ],
)
def test_schema_rejects_wrong_or_unknown_contract_shape(revision: str, auth: dict[str, str]):
    with pytest.raises(jsonschema.ValidationError):
        validate(manifest(revision, auth))
