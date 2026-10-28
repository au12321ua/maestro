"""Model configurations and harness versions (03 section 2).

Administrators keep both in a web session. A model configuration changes by revision;
a run freezes the revision it used, so later changes never rewrite it (CFG-05). A
harness version is fixed once published; a change is a new version. Everyone may read
both; only administrators see which credential a model configuration is bound to.
"""

from __future__ import annotations

import re

from sqlalchemy import Connection, insert, select, update

from maestro.core import clock
from maestro.core.digests import canonical_json, digest_json
from maestro.core.errors import FieldError, MaestroError, not_found
from maestro.core.ids import new_id
from maestro.db import schema as t
from maestro.db.engine import Database
from maestro.execution.agents import (
    PARAM_TYPES,
    PARAMS_MAX_BYTES,
    PROTOCOLS,
    UPSTREAM_VERSION,
    endpoint_problem,
    param_problem,
    secret_problems,
)
from maestro.services import audit, deleted
from maestro.services.credentials import credential_problem
from maestro.services.principal import Principal, require_admin_session
from maestro.services.upstream import Unavailable, UpstreamVersions

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
PROVIDER = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}")
PARAM_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
MODEL_KINDS = ("model_api_key",)


def _invalid(code: str, message: str, errors: list[FieldError]) -> MaestroError:
    return MaestroError(code, message, 422, field_errors=errors)


def _check_params(value, path: str, errors: list[FieldError]) -> dict:
    if not isinstance(value, dict):
        errors.append(FieldError(path, "TYPE", "an object"))
        return {}
    try:
        size = len(canonical_json(value))
    except (TypeError, ValueError):
        errors.append(FieldError(path, "INVALID", "plain JSON with finite numbers"))
        return {}
    if size > PARAMS_MAX_BYTES:
        errors.append(FieldError(path, "TOO_LARGE", limit=PARAMS_MAX_BYTES))
    for pointer in secret_problems(value):
        errors.append(
            FieldError(
                path + pointer,
                "LOOKS_LIKE_SECRET",
                "credentials go in a team credential, bound by reference",
            )
        )
    return value


def _check_model(body: dict) -> dict:
    errors: list[FieldError] = []
    provider = body.get("provider")
    if not isinstance(provider, str) or not PROVIDER.fullmatch(provider):
        errors.append(FieldError("/provider", "PATTERN", "lowercase letters, digits, . _ -"))
    if body.get("protocol") not in PROTOCOLS:
        errors.append(FieldError("/protocol", "ENUM", ", ".join(PROTOCOLS)))
    problem = endpoint_problem(body.get("endpoint"))
    if problem:
        errors.append(FieldError("/endpoint", problem, "http(s) URL without credentials"))
    model_id = body.get("model_id")
    if (
        not isinstance(model_id, str)
        or not 1 <= len(model_id) <= 256
        or any(c.isspace() for c in model_id)
    ):
        errors.append(FieldError("/model_id", "INVALID", "1-256 characters, no spaces"))
    params = _check_params(body.get("params") or {}, "/params", errors)
    credential_id = body.get("credential_id")
    if credential_id is not None and not isinstance(credential_id, str):
        errors.append(FieldError("/credential_id", "TYPE"))
    description = body.get("description", "")
    if not isinstance(description, str) or len(description) > 512:
        errors.append(FieldError("/description", "LENGTH"))
    if errors:
        raise _invalid("MODEL_CONFIG_INVALID", "Invalid model configuration", errors)
    return {
        "provider": provider,
        "protocol": body["protocol"],
        "endpoint": body["endpoint"],
        "model_id": model_id,
        "params": params,
        "credential_id": credential_id,
        "description": description,
    }


def _bind_credential(conn: Connection, credential_id: str | None) -> None:
    if credential_id is None:
        return
    problem = credential_problem(conn, credential_id, MODEL_KINDS)
    if problem:
        raise _invalid(
            "MODEL_CONFIG_INVALID",
            "The credential cannot be bound",
            [FieldError("/credential_id", problem, "an enabled model API key")],
        )


def check_harness_schema(schema) -> dict:
    """``{"parameters": {NAME: {"type", "description"?, "default"?, "enum"?,
    "minimum"?, "maximum"?}}}``; unknown parameters are refused at resolution."""
    errors: list[FieldError] = []
    if not isinstance(schema, dict) or set(schema) - {"parameters"}:
        raise _invalid(
            "HARNESS_INVALID",
            "Parameter schema must be {'parameters': {...}}",
            [FieldError("/parameter_schema", "INVALID")],
        )
    parameters = schema.get("parameters") or {}
    if not isinstance(parameters, dict) or len(parameters) > 64:
        errors.append(FieldError("/parameter_schema/parameters", "INVALID"))
        parameters = {}
    for name, spec in parameters.items():
        path = f"/parameter_schema/parameters/{name}"
        if not PARAM_NAME.fullmatch(name):
            errors.append(FieldError(path, "PATTERN"))
            continue
        if not isinstance(spec, dict) or spec.get("type") not in PARAM_TYPES:
            errors.append(FieldError(path + "/type", "ENUM", ", ".join(sorted(PARAM_TYPES))))
            continue
        extra = set(spec) - {"type", "description", "default", "enum", "minimum", "maximum"}
        if extra:
            errors.append(FieldError(path, "UNKNOWN_KEYS", ", ".join(sorted(extra))))
        if "enum" in spec and (not isinstance(spec["enum"], list) or not spec["enum"]):
            errors.append(FieldError(path + "/enum", "INVALID"))
        for bound in ("minimum", "maximum"):
            if bound in spec and (
                spec["type"] not in {"integer", "number"}
                or param_problem({"type": "number"}, spec[bound])
            ):
                errors.append(FieldError(f"{path}/{bound}", "INVALID"))
        if "default" in spec and param_problem(spec, spec["default"]):
            errors.append(FieldError(path + "/default", "INVALID"))
        if not isinstance(spec.get("description", ""), str):
            errors.append(FieldError(path + "/description", "TYPE"))
    _check_params(parameters, "/parameter_schema/parameters", errors)
    if errors:
        raise _invalid("HARNESS_INVALID", "Invalid parameter schema", errors)
    return {"parameters": parameters}


def harness_digest(
    agent: str,
    name: str,
    version: str,
    protocols: list,
    schema: dict,
    upstream_version: str | None = None,
) -> str:
    content = {
        "agent": agent,
        "name": name,
        "version": version,
        "protocols": protocols,
        "schema": schema,
    }
    if upstream_version is not None:
        content["upstream_version"] = upstream_version
    return digest_json(content)


class ModelConfigs:
    def __init__(
        self,
        db: Database,
        adapters=None,
        parameters=None,
        model_parameters=None,
        upstream=None,
        versions: UpstreamVersions | None = None,
    ):
        self.db = db
        # Executor adapters: name -> protocols; None when no executor is configured.
        self.adapters = adapters
        # Parameters each adapter accepts; an adapter not listed accepts any.
        self.parameters = parameters or {}
        # Model configuration parameters each adapter passes on; one not listed: any.
        self.model_parameters = model_parameters or {}
        # Adapters that install a program: name -> {"package", "tested"} (N-09).
        self.upstream = upstream or {}
        self.versions = versions

    # --- model configurations -------------------------------------------------------

    @staticmethod
    def _public_model(row, revision, principal: Principal, credentials: dict) -> dict:
        cred = credentials.get(revision["credential_id"]) if revision["credential_id"] else None
        public = {
            "id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "enabled": row["enabled"],
            "revision": row["current_revision"],
            "provider": revision["provider"],
            "protocol": revision["protocol"],
            "endpoint": revision["endpoint"],
            "model_id": revision["model_id"],
            "params": revision["params_json"],
            # Members see whether a key is bound and usable, not which one.
            "credential_bound": revision["credential_id"] is not None,
            "credential_problem": cred["problem"] if cred else None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        if principal.is_admin_session:
            public["credential_id"] = revision["credential_id"]
            public["credential_name"] = cred["name"] if cred else None
        return public

    @staticmethod
    def _credentials(conn: Connection, ids) -> dict:
        ids = [x for x in set(ids) if x]
        names = dict(
            conn.execute(
                select(t.credentials.c.id, t.credentials.c.name).where(t.credentials.c.id.in_(ids))
            ).all()
        )
        return {
            x: {"name": names.get(x), "problem": credential_problem(conn, x, MODEL_KINDS)}
            for x in ids
        }

    def _current(self, conn: Connection, rows) -> dict:
        ids = [r["id"] for r in rows]
        revisions = {}
        for rev in conn.execute(
            select(t.model_revisions).where(t.model_revisions.c.model_config_id.in_(ids))
        ).mappings():
            revisions[(rev["model_config_id"], rev["revision"])] = rev
        return {r["id"]: revisions[(r["id"], r["current_revision"])] for r in rows}

    def list_models(self, principal: Principal, include_disabled: bool = True) -> list[dict]:
        with self.db.read() as conn:
            query = (
                select(t.model_configs)
                .where(deleted.visible(t.model_configs.c.id, "model_config"))
                .order_by(t.model_configs.c.name)
            )
            if not include_disabled:
                query = query.where(t.model_configs.c.enabled.is_(True))
            rows = conn.execute(query).mappings().all()
            current = self._current(conn, rows)
            creds = self._credentials(conn, [c["credential_id"] for c in current.values()])
            return [self._public_model(r, current[r["id"]], principal, creds) for r in rows]

    def _model_row(self, conn: Connection, config_id: str):
        row = (
            conn.execute(select(t.model_configs).where(t.model_configs.c.id == config_id))
            .mappings()
            .first()
        )
        if row is None:
            raise not_found("Model configuration")
        return row

    def get_model(self, principal: Principal, config_id: str) -> dict:
        with self.db.read() as conn:
            row = self._model_row(conn, config_id)
            deleted.refuse_purged(conn, "model_config", config_id, "Model configuration")
            revisions = (
                conn.execute(
                    select(t.model_revisions)
                    .where(t.model_revisions.c.model_config_id == config_id)
                    .order_by(t.model_revisions.c.revision.desc())
                )
                .mappings()
                .all()
            )
            current = next(r for r in revisions if r["revision"] == row["current_revision"])
            creds = self._credentials(conn, [current["credential_id"]])
            public = self._public_model(row, current, principal, creds)
            public["deletion"] = deleted.state(conn, "model_config", config_id)
            public["revisions"] = [
                {
                    "revision": r["revision"],
                    "provider": r["provider"],
                    "protocol": r["protocol"],
                    "endpoint": r["endpoint"],
                    "model_id": r["model_id"],
                    "params": r["params_json"],
                    "credential_bound": r["credential_id"] is not None,
                    **({"credential_id": r["credential_id"]} if principal.is_admin_session else {}),
                    "created_at": r["created_at"],
                }
                for r in revisions
            ]
            return public

    def _insert_revision(self, conn, config_id, revision, fields, principal, at) -> None:
        conn.execute(
            insert(t.model_revisions).values(
                id=new_id("mrev"),
                model_config_id=config_id,
                revision=revision,
                provider=fields["provider"],
                protocol=fields["protocol"],
                endpoint=fields["endpoint"],
                model_id=fields["model_id"],
                params_json=fields["params"],
                credential_id=fields["credential_id"],
                created_by=principal.user_id,
                created_at=at,
            )
        )

    def create_model(self, principal: Principal, body: dict) -> dict:
        require_admin_session(principal)
        name = body.get("name")
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise _invalid(
                "MODEL_CONFIG_INVALID",
                "Invalid model configuration",
                [FieldError("/name", "PATTERN", "letters, digits, . _ -")],
            )
        fields = _check_model(body)
        config_id = new_id("mdl")
        at = clock.to_text(clock.now())
        with self.db.write() as conn:
            taken = conn.execute(
                select(t.model_configs.c.id).where(
                    t.model_configs.c.name == name,
                    t.model_configs.c.id.not_in(
                        select(t.deletions.c.object_id).where(
                            t.deletions.c.object_type == "model_config",
                            t.deletions.c.state == deleted.PURGED,
                        )
                    ),
                )
            ).first()
            if taken:
                raise MaestroError(
                    "MODEL_CONFIG_NAME_TAKEN",
                    "Name already used (also by configurations in the recycle bin)",
                    409,
                    field_errors=[FieldError("/name", "TAKEN")],
                )
            _bind_credential(conn, fields["credential_id"])
            conn.execute(
                insert(t.model_configs).values(
                    id=config_id,
                    name=name,
                    description=fields["description"],
                    enabled=True,
                    current_revision=1,
                    created_by=principal.user_id,
                    created_at=at,
                    updated_at=at,
                )
            )
            self._insert_revision(conn, config_id, 1, fields, principal, at)
            audit.record(
                conn,
                principal,
                "model_config.create",
                object_type="model_config",
                object_id=config_id,
                detail={k: fields[k] for k in ("provider", "protocol", "model_id")},
            )
        return self.get_model(principal, config_id)

    def _live_model(self, conn: Connection, config_id: str):
        row = self._model_row(conn, config_id)
        if deleted.state(conn, "model_config", config_id):
            raise MaestroError("MODEL_CONFIG_DELETED", "Model configuration was deleted", 409)
        return row

    def revise_model(
        self, principal: Principal, config_id: str, body: dict, expected_revision: int
    ) -> dict:
        """A new revision replaces every field; accepted runs keep theirs (CFG-05)."""
        require_admin_session(principal)
        fields = _check_model(body)
        at = clock.to_text(clock.now())
        with self.db.write() as conn:
            row = self._live_model(conn, config_id)
            if row["current_revision"] != expected_revision:
                raise MaestroError(
                    "REVISION_CONFLICT", "Model configuration changed; reload and edit again", 409
                )
            _bind_credential(conn, fields["credential_id"])
            revision = row["current_revision"] + 1
            self._insert_revision(conn, config_id, revision, fields, principal, at)
            conn.execute(
                update(t.model_configs)
                .where(t.model_configs.c.id == config_id)
                .values(current_revision=revision, description=fields["description"], updated_at=at)
            )
            audit.record(
                conn,
                principal,
                "model_config.revise",
                object_type="model_config",
                object_id=config_id,
                detail={"revision": revision},
            )
        return self.get_model(principal, config_id)

    def set_model_enabled(self, principal: Principal, config_id: str, enabled: bool) -> dict:
        """Disabling stops new runs from choosing it; accepted runs keep running."""
        require_admin_session(principal)
        with self.db.write() as conn:
            row = self._live_model(conn, config_id)
            if row["enabled"] != enabled:
                conn.execute(
                    update(t.model_configs)
                    .where(t.model_configs.c.id == config_id)
                    .values(enabled=enabled, updated_at=clock.to_text(clock.now()))
                )
                audit.record(
                    conn,
                    principal,
                    f"model_config.{'enable' if enabled else 'disable'}",
                    object_type="model_config",
                    object_id=config_id,
                )
        return self.get_model(principal, config_id)

    # --- harness versions -------------------------------------------------------

    def public_adapters(self) -> list[dict]:
        return [
            {
                "agent": name,
                "protocols": sorted(protocols),
                "parameters": sorted(self.parameters[name]) if name in self.parameters else None,
                "model_parameters": (
                    sorted(self.model_parameters[name]) if name in self.model_parameters else None
                ),
                "package": self.upstream.get(name, {}).get("package"),
                "tested_version": self.upstream.get(name, {}).get("tested"),
            }
            for name, protocols in sorted((self.adapters or {}).items())
        ]

    def upstream_versions(self, principal: Principal, agent: str, refresh: bool = False) -> dict:
        """What the adapter's package registry lists, for choosing an upstream version;
        a failed lookup is reported in ``error``, not raised (the form still works)."""
        require_admin_session(principal)
        if agent not in self.upstream or agent not in (self.adapters or {}):
            raise not_found("Adapter that installs a program")
        found = {
            "agent": agent,
            "package": self.upstream[agent]["package"],
            "tested_version": self.upstream[agent].get("tested"),
            "registry": None,
            "checked_at": None,
            "tags": [],
            "recent": [],
            "error": None,
        }
        if self.versions is None:
            return {**found, "error": "No registry lookup in this service"}
        try:
            summary = self.versions.lookup(found["package"], refresh=refresh)
        except Unavailable as error:
            return {**found, "error": str(error)}
        return {**found, **{k: v for k, v in summary.items() if k != "versions"}}

    @staticmethod
    def _public_harness(row) -> dict:
        return {
            "id": row["id"],
            "name": row["name"],
            "version": row["version"],
            "agent": row["agent"],
            "upstream_version": row["upstream_version"],
            "description": row["description"],
            "protocols": row["protocols_json"],
            "parameter_schema": row["schema_json"],
            "content_digest": row["content_digest"],
            "enabled": row["enabled"],
            "created_at": row["created_at"],
        }

    def list_harnesses(self, principal: Principal) -> list[dict]:
        with self.db.read() as conn:
            rows = conn.execute(
                select(t.harness_versions)
                .where(deleted.visible(t.harness_versions.c.id, "harness_version"))
                .order_by(t.harness_versions.c.name, t.harness_versions.c.created_at)
            ).mappings()
            return [self._public_harness(r) for r in rows]

    def _harness_row(self, conn: Connection, harness_id: str):
        row = (
            conn.execute(select(t.harness_versions).where(t.harness_versions.c.id == harness_id))
            .mappings()
            .first()
        )
        if row is None:
            raise not_found("Harness version")
        return row

    def get_harness(self, principal: Principal, harness_id: str) -> dict:
        with self.db.read() as conn:
            row = self._harness_row(conn, harness_id)
            deleted.refuse_purged(conn, "harness_version", harness_id, "Harness version")
            return {
                **self._public_harness(row),
                "deletion": deleted.state(conn, "harness_version", harness_id),
            }

    def create_harness(self, principal: Principal, body: dict) -> dict:
        require_admin_session(principal)
        errors: list[FieldError] = []
        name, version, agent = body.get("name"), body.get("version"), body.get("agent")
        if not isinstance(name, str) or not NAME.fullmatch(name):
            errors.append(FieldError("/name", "PATTERN"))
        if not isinstance(version, str) or not VERSION.fullmatch(version):
            errors.append(FieldError("/version", "PATTERN"))
        adapters = self.adapters or {}
        if agent not in adapters:
            errors.append(
                FieldError("/agent", "UNSUPPORTED", ", ".join(sorted(adapters)) or "none")
            )
        protocols = body.get("protocols")
        if (
            not isinstance(protocols, list)
            or not protocols
            or len(set(protocols)) != len(protocols)
            or any(p not in PROTOCOLS for p in protocols)
        ):
            errors.append(FieldError("/protocols", "INVALID", ", ".join(PROTOCOLS)))
        elif agent in adapters and set(protocols) - set(adapters[agent]):
            errors.append(
                FieldError("/protocols", "UNSUPPORTED", ", ".join(sorted(adapters[agent])))
            )
        description = body.get("description", "")
        if not isinstance(description, str) or len(description) > 512:
            errors.append(FieldError("/description", "LENGTH"))
        upstream_version = body.get("upstream_version")
        if agent in self.upstream:
            if (
                not isinstance(upstream_version, str)
                or len(upstream_version) > 64
                or not UPSTREAM_VERSION.fullmatch(upstream_version)
            ):
                errors.append(
                    FieldError("/upstream_version", "PATTERN", "an exact version such as 1.2.3")
                )
        elif upstream_version is not None:
            errors.append(
                FieldError("/upstream_version", "UNSUPPORTED", f"{agent} installs nothing")
            )
        if errors:
            raise _invalid("HARNESS_INVALID", "Invalid harness version", errors)
        if agent in self.upstream and self.versions is not None:
            package = self.upstream[agent]["package"]
            if self.versions.exists(package, upstream_version) is False:
                raise _invalid(
                    "HARNESS_INVALID",
                    f"{package} has no version {upstream_version}",
                    [FieldError("/upstream_version", "NOT_FOUND", package)],
                )
        schema = check_harness_schema(body.get("parameter_schema") or {"parameters": {}})
        allowed = self.parameters.get(agent)
        unknown = sorted(set(schema["parameters"]) - allowed) if allowed is not None else []
        if unknown:
            raise _invalid(
                "HARNESS_INVALID",
                f"{agent} does not take these parameters",
                [
                    FieldError(f"/parameter_schema/parameters/{name}", "UNSUPPORTED")
                    for name in unknown
                ],
            )
        protocols = sorted(protocols)
        harness_id = new_id("hrn")
        with self.db.write() as conn:
            taken = conn.execute(
                select(t.harness_versions.c.id).where(
                    t.harness_versions.c.name == name,
                    t.harness_versions.c.version == version,
                    t.harness_versions.c.id.not_in(
                        select(t.deletions.c.object_id).where(
                            t.deletions.c.object_type == "harness_version",
                            t.deletions.c.state == deleted.PURGED,
                        )
                    ),
                )
            ).first()
            if taken:
                raise MaestroError(
                    "HARNESS_VERSION_EXISTS",
                    "This name and version are published; publish a new version to change it",
                    409,
                    field_errors=[FieldError("/version", "TAKEN")],
                )
            conn.execute(
                insert(t.harness_versions).values(
                    id=harness_id,
                    name=name,
                    version=version,
                    agent=agent,
                    upstream_version=upstream_version,
                    description=description,
                    protocols_json=protocols,
                    schema_json=schema,
                    content_digest=harness_digest(
                        agent, name, version, protocols, schema, upstream_version
                    ),
                    enabled=True,
                    created_by=principal.user_id,
                    created_at=clock.to_text(clock.now()),
                )
            )
            audit.record(
                conn,
                principal,
                "harness.create",
                object_type="harness_version",
                object_id=harness_id,
                detail={
                    "name": name,
                    "version": version,
                    "agent": agent,
                    "upstream_version": upstream_version,
                },
            )
        return self.get_harness(principal, harness_id)

    def set_harness_enabled(self, principal: Principal, harness_id: str, enabled: bool) -> dict:
        require_admin_session(principal)
        with self.db.write() as conn:
            row = self._harness_row(conn, harness_id)
            if deleted.state(conn, "harness_version", harness_id):
                raise MaestroError("HARNESS_DELETED", "Harness version was deleted", 409)
            if row["enabled"] != enabled:
                conn.execute(
                    update(t.harness_versions)
                    .where(t.harness_versions.c.id == harness_id)
                    .values(enabled=enabled)
                )
                audit.record(
                    conn,
                    principal,
                    f"harness.{'enable' if enabled else 'disable'}",
                    object_type="harness_version",
                    object_id=harness_id,
                )
        return self.get_harness(principal, harness_id)
