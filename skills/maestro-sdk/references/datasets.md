# Uploading datasets and choosing task environments

Read this before uploading local tasks or changing their environment. Upload a directory whose immediate children are Harbor task directories. Convert benchmark material locally first: the service accepts converted tasks and does not run benchmark converters.

## Upload, wait, then check admission

Check the calling PAT without printing the token:

```python
me = client.me()
if me.auth_channel != "pat" or me.role != "admin" or "datasets" not in (me.pat_permissions or []):
    raise RuntimeError("Upload needs an administrator PAT with datasets permission")
```

If it lacks permission, ask an administrator to upload or supply an appropriate PAT. Do not expand permissions as a routine setup step. Members, credentials and models are still administered through the web.

```python
operation = client.catalog.upload(
    "tasks", namespace="team", name="geometry-tools", label="extra-dependencies",
    based_on=None,  # or an undeleted version id in this same uploaded dataset
)
imported = client.operations.wait(operation.id, timeout=3600)
if imported.status != "succeeded" or imported.result is None:
    raise RuntimeError(f"Import ended as {imported.status}: {imported.error}")
result = imported.result
version_id = result.dataset_version_id
print("existing:", result.existing, "ignored:", result.ignored)
version = client.catalog.version(version_id)
preparation_id = result.preparation_operation_id or version.first_preparation_operation_id
if preparation_id is not None:
    prepared = client.operations.wait(preparation_id, timeout=3600)
    print(prepared.status, prepared.error, prepared.result)
for task in client.catalog.tasks(version_id):
    print(task.task_key, task.admission.state if task.admission else None,
          task.can_submit, task.blockers)
```

Operation models expose their fields directly (`operation.id`, `imported.result`). Waiting returns at any terminal status, including failure, cancellation and interruption; it does not imply success. `WaitTimeout` stops waiting while the service continues. Inspect the import's progress and error for task validation failures, and the preparation's result for individual task outcomes. Upload success does not mean every task is admitted: check each task's admission and blockers, then resolve the intended run configuration before submitting. A missing preparation id is not evidence that tasks are ready.

The same content uploaded again within this dataset returns the existing version (`existing=True`). Supplied `label` and `based_on` may appear in `ignored`; those values were not applied to the existing version. Do not report a new version or changed provenance from such an upload.

## Record provenance on the version

Use a workflow record attached to the dataset version, with Markdown in `text` for people and optional structured facts in `body` for programs:

```python
record = client.records.create(
    {"type": "dataset_version", "id": version_id},
    "provenance", "Task source and environment changes",
    text="Upstream: <repository and revision>. Conversion: <tool and command>. "
         "Environment changes: <what changed and why>.",
)
records = client.records.list("dataset_version", version_id)
```

`maestro_sdk.workflows` provides helpers that compose public API calls; dataset provenance uses `client.records` directly. It needs the PAT's `experiment` permission in addition to the upload's `datasets` permission. Have an authorised team member write the record if necessary. Keep secrets out of task files and record text.

## Choose the environment or an attachment

| Need | Use | Platform preparation |
| --- | --- | --- |
| An existing usable image | `[environment] docker_image = "..."` in `task.toml` | Pulls and pins the registry digest |
| System dependencies or environment files | `environment/Dockerfile` and its build files | Builds the image |
| A helper service with its own image | `build:` in `environment/docker-compose.yaml` | Builds the helper alongside the main environment |
| Switchable skills or tools for the agent under test | An attachment with `bootstrap.sh` | Installs per trial; shared preparation stays the same |

The platform stores prepared images in its local registry. Never push task images to Docker Hub as part of this workflow. Base images and referenced images must be pullable by the service host; private registry authentication is supplied by the deployment environment, not passwords in task files. A locally built image on your workstation is not automatically available to the service. If both `docker_image` and `environment/Dockerfile` exist, `docker_image` takes precedence; remove that setting when switching to a Dockerfile build.

### Minimal Dockerfile task

For a new task, the upload root can look like this (keep the task's instruction, solution and checks appropriate to your benchmark):

```text
tasks/
  example/
    task.toml
    instruction.md
    environment/Dockerfile
    solution/solve.sh
    tests/test.sh
```

Minimal `task.toml`:

```toml
schema_version = "1.4"

[environment]
os = "linux"
```

`environment/Dockerfile` adds a system dependency:

```dockerfile
FROM ubuntu:22.04
RUN apt-get update && apt-get install -y --no-install-recommends jq \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
```

Upload `tasks`, not `tasks/example`. For an existing task, preserve its other files and settings and change only the environment you intend to test. Build files used by `COPY` must be in `environment/`.

### Compose helper with a build

Keep the main Dockerfile above and add:

```text
example/environment/
  Dockerfile
  docker-compose.yaml
  helper/Dockerfile
```

`environment/docker-compose.yaml`:

```yaml
services:
  main:
    depends_on:
      - helper
  helper:
    build:
      context: ./helper
      dockerfile: Dockerfile
      args:
        PYTHON_VERSION: "${PYTHON_VERSION:-3.12}"
```

`environment/helper/Dockerfile`:

```dockerfile
ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim
WORKDIR /srv
RUN printf 'helper ready\n' > index.html
CMD ["python", "-m", "http.server", "8000"]
```

The main container can reach this helper as `helper:8000`. The platform supplies `main`'s image from the task; do not put `image:` or `build:` on Compose `main`. Each helper must have an `image:` or `build:` definition.

### Preparation limits

- The build context must be a directory inside the corresponding environment directory (`environment/`, or `tests/` for a separate verifier). Its Dockerfile must be a file inside that context. Do not reference files outside it. Symlinks and special files are unsupported task content.
- Supported content options in `build:` are `context`, `dockerfile`, `args` and `target`. `labels`, `cache_from`, `cache_to` and `tags` are accepted but ignored by preparation. Other build options are unsupported, including build secrets, SSH forwarding and additional contexts. Every build argument needs an explicit value.
- Variables in `image:` and `build:` are frozen to their written defaults, such as `${VERSION:-1}` or `${VERSION-1}`. Defaults cannot contain another variable, and the same variable must have consistent defaults. Variables without defaults, or names resembling secrets (`KEY`, `SECRET`, `TOKEN`, `PASSWORD`, `CREDENTIAL`, `AUTH`), cannot be prepared. Run parameters cannot change the prepared images.
- Every Compose service must have a fixed single replica. Multiple replicas, variable replica counts and global deployment mode are unsupported.
- Ordinary internal volumes and tmpfs are supported. External resources and explicitly named networks or volumes, non-local volume drivers and volume driver options, host bind mounts, `volumes_from`, service configs or secrets, device mounts, Docker API sockets and privileged services are unsupported. These checks also apply to `main` and separate verifier services.
- Separate verifiers for multi-step tasks and step-specific environments are unsupported.

## Versions and comparisons

Changing environment files changes task content and produces a different version on upload. Executable file bits also participate in content identity. An upload dataset gets further versions by upload; `based_on` can only name an undeleted version in that same uploaded dataset, and records a source relationship rather than inheriting files.

A Hub dataset gets versions only from Hub. To change Hub tasks, obtain and convert the required material locally and upload under a new dataset name. Do not upload into a Hub dataset's name or use a version from another dataset as `based_on`.

Changing the task environment changes the experiment conditions. It is not an attachment-only candidate for an attachment A/B collection. Comparisons across different dataset content versions show raw summaries side by side only: no task-name alignment or score differences across versions. Reuse of task-read summaries or held-out marks does not enable aligned cross-version comparison.
