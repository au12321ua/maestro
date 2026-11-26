export interface AttachmentDirectoryNavigation {
  previousAttachmentPages: (string | null)[];
}

export function directoryCursor(params: URLSearchParams): string | null {
  return params.getAll("page").at(-1) ?? null;
}

export type AttachmentKind = "attachment" | "submission";

export function attachmentKind(params: URLSearchParams): AttachmentKind {
  return params.get("kind") === "submission" ? "submission" : "attachment";
}

export function attachmentPath(
  name: string,
  params: URLSearchParams,
  kind: AttachmentKind,
  snapshot?: string,
  base?: string,
) {
  const next = directoryParams(params);
  next.set("kind", kind);
  if (snapshot) next.set("snapshot", snapshot);
  if (base !== undefined) next.set("base", base);
  return `/attachments/${encodeURIComponent(name)}?${next}`;
}

export function directoryParams(params: URLSearchParams) {
  const next = new URLSearchParams(params);
  for (const field of ["name", "snapshot", "base"]) next.delete(field);
  const cursor = directoryCursor(params);
  if (cursor) next.set("page", cursor);
  return next;
}
