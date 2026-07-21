/** Replaces {"$data": "<ref_id>"} placeholders with arrays fetched from the server. */

const cache = new Map<string, Promise<any[]>>();

function fetchArray(refId: string): Promise<any[]> {
  let p = cache.get(refId);
  if (!p) {
    p = fetch(`/data/${refId}`)
      .then((r) => {
        if (!r.ok) throw new Error(`data ref ${refId} not found`);
        return r.json();
      })
      .then((j) => j.values as any[]);
    cache.set(refId, p);
  }
  return p;
}

function collect(node: any, out: Set<string>) {
  if (Array.isArray(node)) {
    node.forEach((n) => collect(n, out));
  } else if (node && typeof node === "object") {
    if (typeof node.$data === "string") out.add(node.$data);
    else Object.values(node).forEach((n) => collect(n, out));
  }
}

function substitute(node: any, arrays: Map<string, any[]>): any {
  if (Array.isArray(node)) return node.map((n) => substitute(n, arrays));
  if (node && typeof node === "object") {
    if (typeof node.$data === "string") return arrays.get(node.$data) ?? [];
    const out: any = {};
    for (const [k, v] of Object.entries(node)) out[k] = substitute(v, arrays);
    return out;
  }
  return node;
}

/** Resolve every $data reference in a spec. Returns a new spec with real arrays. */
export async function hydrate(spec: any): Promise<any> {
  const ids = new Set<string>();
  collect(spec, ids);
  if (ids.size === 0) return spec;
  const entries = await Promise.all(
    [...ids].map(async (id) => [id, await fetchArray(id)] as const)
  );
  return substitute(spec, new Map(entries));
}
