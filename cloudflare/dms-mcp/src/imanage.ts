/**
 * iManage Work, as the lawyer whose token this is. A port of the container's
 * iManage client (src/lra/dms/imanage.py, removed in docs/migration.md,
 * phase 5): the same paths, the same fields, the same refusal on 401/403. Written from iManage's documentation; no real instance has
 * answered it yet, and the REST paths vary by deployment version.
 */

export interface DmsDocument {
  docId: string;
  name: string;
  matterId: string | null;
  matterName: string | null;
  version: number;
  modifiedAt: string;
  author: string | null;
  excerpt: string;
}

/** The lawyer's own token was refused. Reported, never worked around. */
export class AccessDenied extends Error {}

const LIBRARY = "/work/api/v2/customers/1/libraries/Active/documents";

export class IManage {
  constructor(
    private readonly baseUrl: string,
    private readonly token: string,
    private readonly fetcher: typeof fetch = fetch,
  ) {}

  async search(query: string, matterId: string, limit: number): Promise<DmsDocument[]> {
    const params = new URLSearchParams({ q: query, limit: String(limit), custom1: matterId });
    const body = (await (await this.get(`${LIBRARY}/search?${params}`)).json()) as {
      data?: Record<string, unknown>[];
    };
    return (body.data ?? []).map(toDocument);
  }

  async text(docId: string): Promise<string> {
    return (await this.get(`${LIBRARY}/${encodeURIComponent(docId)}/download`)).text();
  }

  async versions(docId: string): Promise<DmsDocument[]> {
    const body = (await (await this.get(`${LIBRARY}/${encodeURIComponent(docId)}/versions`)).json()) as {
      data?: Record<string, unknown>[];
    };
    return (body.data ?? []).map(toDocument);
  }

  private async get(path: string): Promise<Response> {
    const response = await this.fetcher(new URL(path, this.baseUrl).toString(), {
      headers: { "X-Auth-Token": this.token, Accept: "application/json" },
    });
    if (response.status === 401 || response.status === 403) {
      throw new AccessDenied("The document system refused that request under your own permissions.");
    }
    if (!response.ok) throw new Error(`the document system answered ${response.status}`);
    return response;
  }
}

function toDocument(d: Record<string, unknown>): DmsDocument {
  const str = (v: unknown) => (v === undefined || v === null ? null : String(v));
  return {
    docId: String(d.id ?? ""),
    name: String(d.name ?? ""),
    matterId: str(d.custom1),
    matterName: str(d.custom1_description),
    version: Number(d.version ?? 1),
    modifiedAt: String(d.edit_date ?? ""),
    author: str(d.author_description),
    excerpt: String(d.content_snippet ?? ""),
  };
}
