/**
 * The three document-system tools, answered for one bound (lawyer, matter).
 *
 * A port of the custom tools in src/lra/tools/__init__.py, with the same
 * rules and the same words, because those words often end up in the email
 * to the lawyer:
 *
 *   - Only the bound matter is searched, listed or read. There is no matter
 *     parameter for the model to change: the matter comes from the signed
 *     binding (binding.ts), never from the tool input, so a counterparty's
 *     draft that says "read doc 4471" cannot reach another client's file.
 *   - A result from another matter is dropped rather than trusted to the
 *     document system's own filter.
 *   - A document is read only when every version of it is filed on the bound
 *     matter. The Python version cached documents it had already seen listed;
 *     this server keeps no state between requests, so it asks every time.
 *   - A long document comes back one window at a time, and says so.
 */

import { AccessDenied, type DmsDocument, IManage } from "./imanage";

export const WINDOW = 60_000;

export const DESCRIPTIONS = {
  search_firm_documents:
    "Search this matter's documents in the firm's document management system, " +
    "as this lawyer. Use this to check how a clause was drafted in an earlier " +
    "draft on this deal or what has already been agreed. Only documents filed " +
    "on the matter this email belongs to are searched; other matters are out " +
    "of reach, and with no matter identified there is nothing to search.",
  read_firm_document:
    "Read a document on this matter, in character ranges for long ones. Use it " +
    "after search_firm_documents or matter_history to compare the document " +
    "under review against a prior version. Documents on other matters cannot " +
    "be read. A long document comes back one window at " +
    "a time; when the result says it was cut short, read the rest before " +
    "concluding anything about what the document does not contain.",
  matter_history:
    "List the documents already filed on this matter, newest first. Use it to " +
    "find prior drafts of the document under review, and to see what has " +
    "already been agreed on this deal.",
} as const;

export class Tools {
  constructor(
    private readonly dms: IManage,
    private readonly matter: string,
    private readonly audit: (tool: string, detail: Record<string, unknown>) => void = () => {},
  ) {}

  async search(query: string, limit = 8): Promise<string> {
    this.audit("search_firm_documents", { query: query.slice(0, 200), limit });
    let docs: DmsDocument[];
    try {
      docs = await this.dms.search(query, this.matter, clamp(limit, 1, 50));
    } catch (e) {
      if (e instanceof AccessDenied) return `Could not search: ${e.message}`;
      throw e;
    }
    docs = docs.filter((d) => d.matterId === this.matter);
    if (!docs.length) return `No documents matched '${query}'.`;
    return docs
      .map(
        (d) =>
          `- ${d.name} (id=${d.docId}, matter=${d.matterName || d.matterId}, ` +
          `v${d.version}, modified ${d.modifiedAt})\n  ${d.excerpt.slice(0, 300)}`,
      )
      .join("\n");
  }

  async history(limit = 15): Promise<string> {
    this.audit("matter_history", { limit });
    let docs: DmsDocument[];
    try {
      docs = await this.dms.search("", this.matter, clamp(limit, 1, 100));
    } catch (e) {
      if (e instanceof AccessDenied) return `Could not list matter documents: ${e.message}`;
      throw e;
    }
    docs = docs.filter((d) => d.matterId === this.matter);
    return (
      docs
        .map((d) => `- ${d.name} (id=${d.docId}, v${d.version}, modified ${d.modifiedAt}, author ${d.author})`)
        .join("\n") || `No documents on matter ${this.matter}.`
    );
  }

  async read(docId: string, start = 0, end = WINDOW): Promise<string> {
    this.audit("read_firm_document", { doc_id: docId, start, end });
    if (!(await this.onMatter(docId))) {
      return (
        `Document ${docId} is not on this matter, so I will not read it. ` +
        "Only documents listed by matter_history or search_firm_documents " +
        "for this matter can be read."
      );
    }
    let text: string;
    try {
      text = await this.dms.text(docId);
    } catch (e) {
      if (e instanceof AccessDenied) return `Could not read that document: ${e.message}`;
      throw e;
    }
    const total = text.length;
    start = Math.max(0, Math.floor(start));
    end = Math.min(Math.max(Math.floor(end), start), start + WINDOW, total);
    if (start >= total) {
      return `That document is ${total} characters long, so there is nothing at character ${start}. Read from 0.`;
    }
    const body = text.slice(start, end);
    if (start === 0 && end === total) return body;
    const head = `[Characters ${start} to ${end} of ${total}.]\n`;
    const tail =
      end < total
        ? `\n\n[Cut off at character ${end} of ${total}. You have not seen ` +
          `the whole document. Call read_firm_document(doc_id="${docId}", ` +
          `start=${end}) to continue, and do not report that something is ` +
          "absent from this document until you have read all of it.]"
        : "";
    return head + body + tail;
  }

  private async onMatter(docId: string): Promise<boolean> {
    try {
      const history = await this.dms.versions(docId);
      return history.length > 0 && history.every((d) => d.matterId === this.matter);
    } catch {
      // Unknown means no.
      return false;
    }
  }
}

function clamp(value: number, low: number, high: number): number {
  return Math.min(Math.max(Math.floor(Number.isFinite(value) ? value : low), low), high);
}
