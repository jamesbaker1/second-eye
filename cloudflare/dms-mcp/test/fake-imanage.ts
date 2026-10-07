/**
 * iManage Work, as far as the adapter asks it anything: search, download and
 * versions, under an X-Auth-Token. Two matters, one document that has been
 * refiled from one to the other, and a token that iManage refuses.
 */

export interface Doc {
  id: string;
  name: string;
  matter: string;
  text: string;
  /** Matters each version is filed on, oldest first. Defaults to [matter]. */
  history?: string[];
}

export const DOCS: Doc[] = [
  { id: "1001", name: "Falcon SPA v3", matter: "FAL-001", text: "Limitation of liability: capped at the price." },
  { id: "1002", name: "Falcon disclosure letter", matter: "FAL-001", text: "x".repeat(150_000) },
  { id: "2001", name: "Heron NDA", matter: "HER-002", text: "Heron's secrets." },
  // Filed on FAL-001 now, on HER-002 before: not wholly this matter's.
  { id: "3001", name: "Moved memo", matter: "FAL-001", text: "moved", history: ["HER-002", "FAL-001"] },
];

export const GOOD_TOKEN = "imanage-token-jim";
export const REFUSED_TOKEN = "imanage-token-revoked";

export interface Recorded {
  url: string;
  token: string | null;
}

export function fakeIManage(docs: Doc[] = DOCS): { fetch: typeof fetch; calls: Recorded[] } {
  const calls: Recorded[] = [];
  const row = (d: Doc, matter = d.matter) => ({
    id: d.id,
    name: d.name,
    custom1: matter,
    custom1_description: `Matter ${matter}`,
    version: 1,
    edit_date: "2026-09-01",
    author_description: "JB",
    content_snippet: d.text.slice(0, 40),
  });
  const fake = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url);
    const token = new Headers(init?.headers).get("x-auth-token");
    calls.push({ url: url.toString(), token });
    if (url.host !== "imanage.test") return new Response("wrong host", { status: 599 });
    if (token === REFUSED_TOKEN) return new Response("no", { status: 401 });
    if (token !== GOOD_TOKEN) return new Response("who", { status: 401 });
    const base = "/work/api/v2/customers/1/libraries/Active/documents";
    if (url.pathname === `${base}/search`) {
      // iManage's own filter is not trusted: it leaks a HER-002 document into
      // every search, and the server must drop it.
      const matter = url.searchParams.get("custom1");
      const q = (url.searchParams.get("q") ?? "").toLowerCase();
      const hits = docs.filter((d) => d.matter === matter && d.text.toLowerCase().includes(q));
      return Response.json({ data: [...hits.map((d) => row(d)), row(docs.find((d) => d.id === "2001")!)] });
    }
    const match = url.pathname.match(new RegExp(`^${base}/([^/]+)/(download|versions)$`));
    const doc = match && docs.find((d) => d.id === decodeURIComponent(match[1]));
    if (!match || !doc) return new Response("not found", { status: 404 });
    if (match[2] === "download") return new Response(doc.text);
    return Response.json({ data: (doc.history ?? [doc.matter]).map((m) => row(doc, m)) });
  };
  return { fetch: fake as typeof fetch, calls };
}
