import { api } from "@/lib/api";
import type { Dataset } from "@/lib/types";

export function datasetPath(datasetId: string, versionId?: string) {
  const path = `/datasets/${encodeURIComponent(datasetId)}`;
  return versionId ? `${path}?version=${encodeURIComponent(versionId)}` : path;
}

/** Import and upload look up the dataset independently of the directory's current scope. */
export async function findDataset(namespace: string, name: string, sourceKind?: string): Promise<Dataset | null> {
  let cursor: string | undefined;
  do {
    const page = await api.get("/datasets", {
      query: { q: `${namespace}/${name}`, include_versions: false, cursor },
    });
    const found = page.data.find((dataset) =>
      dataset.namespace === namespace && dataset.name === name &&
      (!sourceKind || dataset.source_kind === sourceKind),
    );
    if (found) return (await api.get("/datasets/{dataset_id}", { path: { dataset_id: found.id } })).data;
    cursor = page.meta.next_cursor ?? undefined;
  } while (cursor);
  return null;
}
