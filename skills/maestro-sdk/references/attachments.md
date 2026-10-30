# Attachments

An attachment is a directory of files (tools, prompts, scripts) given to the agent under test. Each publish creates an immutable snapshot.

## Publish

```python
a = client.attachments.publish("my-tools", label="v2", parent=previous)
```

- The directory needs a `manifest.json` without a `version` field. A `bootstrap.sh` at the root is the install script each trial runs in its own container.
- Only files the service lacks are uploaded. Publishing the same content again returns the existing snapshot, and its label stays as first given.
- `.git` is left out by default (`exclude=`). Links and special files are refused. Nothing in the directory runs on upload.
- `label` names the snapshot within its attachment and never moves. `parent` records the snapshot this one was made from.

Then put the snapshot id in a configuration: `"attachments": [a.id]`.

## Restore, inspect, compare

- `client.attachments.fetch(a.id, "dir")` makes `dir` hold the snapshot, downloading only files that are missing or different. `prune=True` also removes extra files and file/directory conflicts.
- `client.attachments.diff(a.id)` lists files added, removed and changed since the parent; pass `base=` to compare with another snapshot.
- `client.attachments.download(a.id, "a.zip")` saves the whole snapshot as a zip.
- `client.attachments.list(name=...)` and `client.attachments.get(id)` find existing snapshots.
