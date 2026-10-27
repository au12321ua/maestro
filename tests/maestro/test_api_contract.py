"""The public contract types what it returns (13 F-20, 10 section 8).

Web types and SDK models are generated from the OpenAPI document, so a field the service
declares as free-form JSON is a field neither client can check. Only values that are
open by design may be free-form; this test keeps that list.
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
JSON_VALUE = {"$ref": "#/components/schemas/JsonValue"}

# (schema, property): why the value is open.
OPEN_FIELDS = {
    ("ModelConfigFields", "params"): "provider request parameters, passed through",
    ("ModelConfigCreate", "params"): "provider request parameters, passed through",
    ("ModelConfigRevise", "params"): "provider request parameters, passed through",
    ("ModelConfig", "params"): "provider request parameters, passed through",
    ("ModelRevision", "params"): "provider request parameters, passed through",
    ("EffectiveModel", "params"): "provider request parameters, passed through",
    ("Attempt", "executor_ref"): "executor-specific identifiers",
    ("AttemptError", "detail"): "executor-specific diagnosis",
    ("Problem", "evidence"): "cause-specific evidence",
    ("ConfigDifference", "before"): "any value of the effective configuration",
    ("ConfigDifference", "after"): "any value of the effective configuration",
    ("PreparedOutputView", "archive"): "environment-specific saved original",
    ("PreparedOutputView", "detail"): "environment-specific preparation record",
    ("RecycleEntry", "detail"): "purge receipt; depends on the object type",
    ("AuditEvent", "detail"): "depends on the audited action",
    ("DeploymentSettings", "values"): "config.toml settings, as src/maestro/config.py defines them",
}
# Objects that carry documented fields plus code-specific extras.
OPEN_OBJECTS = {"Problem", "SuggestedAction", "ScoringContract", "ScoringContractInput"}


def open_values(schema: dict) -> bool:
    """A schema that admits any JSON value, directly or as the values of a map."""
    if schema == JSON_VALUE or schema == {}:
        return True
    if schema.get("type") == "object" and "properties" not in schema:
        extra = schema.get("additionalProperties", True)
        return extra is True or extra == {} or extra == JSON_VALUE
    return any(open_values(s) for s in schema.get("anyOf", []) + schema.get("oneOf", []))


def test_only_fields_open_by_design_are_free_form():
    document = json.loads((ROOT / "openapi" / "maestro-v1.json").read_text())
    found = set()
    for name, schema in document["components"]["schemas"].items():
        if name == "JsonValue":
            continue
        if schema.get("additionalProperties") is True:
            assert name.replace("-Input", "").replace("-Output", "") in OPEN_OBJECTS, name
        for prop, value in (schema.get("properties") or {}).items():
            if open_values(value):
                found.add((name.replace("-Input", "").replace("-Output", ""), prop))
    assert found <= set(OPEN_FIELDS), sorted(found - set(OPEN_FIELDS))


def test_every_response_has_a_declared_schema():
    """Except downloads, which are files and are declared as such."""
    document = json.loads((ROOT / "openapi" / "maestro-v1.json").read_text())
    for path, methods in document["paths"].items():
        for method, operation in methods.items():
            for status, response in operation["responses"].items():
                if status == "default" or status == "204":
                    continue
                content = response.get("content")
                if not content:
                    continue
                schema = (content.get("application/json") or {}).get("schema")
                binary = set(content) - {"application/json"}
                assert schema or binary, (method, path, status)
