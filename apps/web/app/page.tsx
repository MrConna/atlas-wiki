"use client";

import { ComponentPropsWithoutRef, FormEvent, useEffect, useRef, useState } from "react";
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";

type Page = { id: string; title: string; slug: string; content: string; source_type: string; category: string | null; updated_at: string };
type PageLinks = { outbound: Page[]; backlinks: Page[] };
type Citation = { page_id: string; chunk_id: string; title: string; excerpt: string; source_location?: string };
type AskResult = { answer: string; evidence: string; citations: Citation[] };

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

// "concepts" and "connections" are the LLM-compiled index layer (cross-note
// concept summaries and the discovered-relationships page) — a map of the
// knowledge base, not part of it. They browse differently: few, referenced
// often, and best scanned at a glance rather than scrolled through.
const INDEX_CATEGORIES = new Set(["concepts", "connections"]);
const formatDate = (iso: string) => iso.slice(0, 10);

// Mirrors the API's slugify() (app/main.py) exactly — page_links resolves
// [[wikilink]]s by slug, so inline rendering has to match by the same rule
// or a link that resolves in the backlinks panel could dead-end here.
function slugifyLike(value: string): string {
  const normalized = value.normalize("NFKC").trim().toLowerCase();
  return normalized.replace(/[^\w\-一-鿿]+/g, "-").replace(/^-+|-+$/g, "") || "untitled";
}

const WIKILINK_PATTERN = /\[\[([^\]|]+)(?:\|([^\]]+))?\]\]/g;

// [[Note Title]] is Obsidian syntax, not markdown — react-markdown just
// renders it as literal text. Rewrite it into a real markdown link with a
// fake `wikilink:` scheme before parsing, then resolve that scheme in a
// custom `a` renderer below.
function preprocessWikilinks(markdown: string): string {
  return markdown.replace(WIKILINK_PATTERN, (_match, target: string, alias?: string) => {
    const label = (alias ?? target).trim();
    return `[${label}](wikilink:${encodeURIComponent(target.trim())})`;
  });
}

// Notes are also full of ordinary external references (X/Twitter posts,
// articles, local project links) that may be dead, moved, or unreachable.
// Opening them in the same tab replaces the whole app with the browser's
// own failure page the moment one doesn't resolve — open those in a new
// tab instead so a bad link never costs the reader their place in the wiki.
// react-markdown also passes a `node` prop (the underlying hast AST node)
// to custom renderers alongside real HTML attributes — it must be pulled
// out here, not spread onto the native <a>, or React stringifies it onto
// the DOM as a bogus node="[object Object]" attribute.
function buildMarkdownComponents(pages: Page[], openPage: (pageId: string, excerpt?: string) => void) {
  return {
    a: ({ href, children, node: _node, ...rest }: ComponentPropsWithoutRef<"a"> & { node?: unknown }) => {
      if (href?.startsWith("wikilink:")) {
        const target = decodeURIComponent(href.slice("wikilink:".length));
        const match = pages.find((page) => page.slug === slugifyLike(target));
        if (!match) return <span className="wikilinkMissing" title="Not found in the wiki">{children}</span>;
        return <button type="button" className="wikilinkButton" onClick={() => openPage(match.id)}>{children}</button>;
      }
      return <a href={href} {...rest} target="_blank" rel="noopener noreferrer">{children}</a>;
    },
  };
}

// wikilink: is not a real protocol — react-markdown's default URL sanitizer
// only allows a fixed safe-scheme allowlist and empties anything else, so
// without this override every wikilink silently loses its href.
function urlTransform(url: string): string {
  return url.startsWith("wikilink:") ? url : defaultUrlTransform(url);
}

export default function Home() {
  const [pages, setPages] = useState<Page[]>([]);
  const [query, setQuery] = useState("");
  const [answer, setAnswer] = useState<AskResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("Ready");
  const [selectedPage, setSelectedPage] = useState<Page | null>(null);
  const [links, setLinks] = useState<PageLinks>({ outbound: [], backlinks: [] });
  const [selectedExcerpt, setSelectedExcerpt] = useState("");
  const [categoryFilter, setCategoryFilter] = useState<string | null>(null);
  const sourcePanelRef = useRef<HTMLElement>(null);

  useEffect(() => {
    if (selectedPage) sourcePanelRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [selectedPage]);

  async function loadPages() {
    try {
      const response = await fetch(`${API}/api/v1/pages`);
      if (!response.ok) throw new Error("Could not load pages");
      setPages(await response.json());
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Could not load pages");
    }
  }

  useEffect(() => { void loadPages(); }, []);

  async function importFile(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const input = form.elements.namedItem("file") as HTMLInputElement;
    if (!input.files?.[0]) return;
    setBusy(true);
    setMessage("Importing…");
    const body = new FormData();
    body.append("file", input.files[0]);
    try {
      const response = await fetch(`${API}/api/v1/imports`, { method: "POST", body });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail ?? "Import failed");
      setMessage(result.duplicate ? "Already indexed — opened existing page." : `Indexed ${result.chunks_created} chunks.`);
      form.reset();
      await loadPages();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Import failed");
    } finally { setBusy(false); }
  }

  async function createPage() {
    const title = window.prompt("Page title");
    if (!title?.trim()) return;
    const content = window.prompt("Initial Markdown content") ?? "";
    const category = window.prompt("Category (optional)") || undefined;
    setBusy(true);
    try {
      const response = await fetch(`${API}/api/v1/pages`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ title, content, category }),
      });
      if (!response.ok) throw new Error("Page creation failed");
      setMessage("Page created.");
      await loadPages();
    } catch (error) { setMessage(error instanceof Error ? error.message : "Page creation failed"); }
    finally { setBusy(false); }
  }

  async function ask(event: FormEvent) {
    event.preventDefault();
    if (!query.trim()) return;
    setBusy(true);
    setAnswer(null);
    setMessage("Searching your sources…");
    try {
      const response = await fetch(`${API}/api/v1/ask`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ question: query }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail ?? "Question failed");
      setAnswer(result);
      setMessage(result.evidence === "insufficient" ? "No supporting evidence found." : "Evidence retrieved.");
    } catch (error) { setMessage(error instanceof Error ? error.message : "Question failed"); }
    finally { setBusy(false); }
  }

  async function editPage(page: Page) {
    const title = window.prompt("Page title", page.title);
    if (!title?.trim()) return;
    const content = window.prompt("Markdown content", page.content);
    if (content === null) return;
    const category = window.prompt("Category (optional)", page.category ?? "") ?? undefined;
    setBusy(true);
    try {
      const response = await fetch(`${API}/api/v1/pages/${page.id}`, {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ title, content, category }),
      });
      if (!response.ok) throw new Error("Page update failed");
      setMessage("Page updated and re-indexed.");
      await loadPages();
    } catch (error) { setMessage(error instanceof Error ? error.message : "Page update failed"); }
    finally { setBusy(false); }
  }

  async function deletePage(page: Page) {
    if (!window.confirm(`Delete “${page.title}” and its search index?`)) return;
    setBusy(true);
    try {
      const response = await fetch(`${API}/api/v1/pages/${page.id}`, { method: "DELETE" });
      if (!response.ok) throw new Error("Page deletion failed");
      setMessage("Page and its index were deleted.");
      await loadPages();
    } catch (error) { setMessage(error instanceof Error ? error.message : "Page deletion failed"); }
    finally { setBusy(false); }
  }

  async function openPage(pageId: string, excerpt = "") {
    setMessage("Opening source…");
    try {
      const [pageResponse, linksResponse] = await Promise.all([
        fetch(`${API}/api/v1/pages/${pageId}`),
        fetch(`${API}/api/v1/pages/${pageId}/links`),
      ]);
      if (!pageResponse.ok || !linksResponse.ok) throw new Error("Source is no longer available");
      setSelectedPage(await pageResponse.json());
      setLinks(await linksResponse.json());
      setSelectedExcerpt(excerpt);
      setMessage("Source opened.");
    } catch (error) { setMessage(error instanceof Error ? error.message : "Could not open source"); }
  }

  const indexPages = pages.filter((page) => page.category && INDEX_CATEGORIES.has(page.category));
  const knowledgePages = pages.filter((page) => !page.category || !INDEX_CATEGORIES.has(page.category));
  const categories = [...new Set(knowledgePages.map((page) => page.category).filter((category): category is string => Boolean(category)))].sort();
  const visiblePages = categoryFilter === null ? knowledgePages : knowledgePages.filter((page) => page.category === categoryFilter);

  return (
    <main>
      <nav>
        <div className="brand"><span>A</span> Atlas Wiki</div>
        <div className="status"><i /> Local-first · {pages.length} pages</div>
      </nav>

      <section className="hero">
        <p className="eyebrow">PERSONAL KNOWLEDGE, WITH PROOF</p>
        <h1>Your second brain should<br />show its sources.</h1>
        <p className="lede">Import documents, connect ideas, and ask questions without losing the trail back to the original words.</p>
        <div className="actions">
          <form onSubmit={importFile} className="importForm">
            <label className="primary">Choose Markdown, TXT, or PDF<input name="file" type="file" accept=".md,.markdown,.txt,.pdf,text/plain,text/markdown,application/pdf" disabled={busy} onChange={(event) => event.currentTarget.form?.requestSubmit()} /></label>
          </form>
          <button className="secondary" onClick={createPage} disabled={busy}>Create a page</button>
        </div>
        <p className="message" role="status">{message}</p>
      </section>

      <section className="workspace">
        <div className="workspaceGrid">
          <div className="main">
            <form className="searchbox" onSubmit={ask}>
              <div className="searchrow"><b>⌕</b><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Ask anything in your knowledge base…" /><button className="ask" disabled={busy}>Ask</button></div>
              <div className="evidence"><span>Atlas answers only when it finds supporting evidence.</span><strong>{pages.length} sources indexed</strong></div>
            </form>

            {answer && <section className="answer">
              <p className="eyebrow">ANSWER · {answer.evidence.replace("_", " ")}</p>
              <div className="answerText">{answer.answer}</div>
              <div className="citations">{answer.citations.map((citation, index) => <article key={citation.chunk_id}>
                <small>[{index + 1}] {citation.source_location}</small><h2>{citation.title}</h2><p>{citation.excerpt}</p><button onClick={() => openPage(citation.page_id, citation.excerpt)}>View exact source</button>
              </article>)}</div>
            </section>}

            {selectedPage && <section className="sourcePanel" ref={sourcePanelRef}>
              <div className="sectionTitle"><p className="eyebrow">SOURCE · {selectedPage.slug}</p><button onClick={() => { setSelectedPage(null); setSelectedExcerpt(""); }}>Close</button></div>
              <h2>{selectedPage.title}</h2>{selectedExcerpt && <blockquote>{selectedExcerpt}</blockquote>}
              <div className="markdown"><ReactMarkdown remarkPlugins={[remarkGfm]} components={buildMarkdownComponents(pages, openPage)} urlTransform={urlTransform}>{preprocessWikilinks(selectedPage.content)}</ReactMarkdown></div>
              {(links.outbound.length > 0 || links.backlinks.length > 0) && <div className="connections">
                <div><b>Links to</b>{links.outbound.map((page) => <button key={page.id} onClick={() => openPage(page.id)}>{page.title}</button>)}</div>
                <div><b>Linked from</b>{links.backlinks.map((page) => <button key={page.id} onClick={() => openPage(page.id)}>{page.title}</button>)}</div>
              </div>}
            </section>}
          </div>

          <aside className="directory library">
            {indexPages.length > 0 && <div className="indexSection">
              <div className="sectionTitle"><p className="eyebrow">🧭 INDEX</p><span>{indexPages.length} 篇</span></div>
              <div className="indexGrid">
                {indexPages.map((page) => <button
                  key={page.id}
                  className={`indexCard${page.category === "connections" ? " indexCardHub" : ""}`}
                  title={page.title}
                  onClick={() => openPage(page.id)}
                >
                  <span className="indexCardIcon">{page.category === "connections" ? "🔗" : "◆"}</span>
                  <span className="indexCardLabel">{page.title}</span>
                </button>)}
              </div>
            </div>}

            <div className="sectionTitle knowledgeHeader"><p className="eyebrow">LIBRARY</p><span>{knowledgePages.length} pages</span></div>
            {categories.length > 0 && <div className="categoryChips">
              <button className={categoryFilter === null ? "active" : ""} onClick={() => setCategoryFilter(null)}>All</button>
              {categories.map((category) => <button key={category} className={categoryFilter === category ? "active" : ""} onClick={() => setCategoryFilter(category)}>{category}</button>)}
            </div>}
            {visiblePages.length === 0 ? <p className="empty">Import a document or create a page to begin.</p> : <div className="directoryList">{visiblePages.map((page) => <div key={page.id} className="directoryItem">
              <button className="directoryOpen" title={page.title} onClick={() => openPage(page.id)}>
                <span className="directoryTitle">{page.title}</span>
                <span className="directoryMeta">
                  <span className="directoryDate">{formatDate(page.updated_at)}</span>
                  {page.category && <span className="categoryBadge">{page.category}</span>}
                </span>
              </button>
              <div className="directoryActions">
                {page.source_type === "manual" && <button onClick={() => editPage(page)} disabled={busy}>Edit</button>}
                <button className="danger" onClick={() => deletePage(page)} disabled={busy}>Delete</button>
              </div>
            </div>)}</div>}
          </aside>
        </div>
      </section>
    </main>
  );
}
