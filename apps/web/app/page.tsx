"use client";

import { ComponentPropsWithoutRef, FormEvent, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

type Page = { id: string; title: string; slug: string; content: string; source_type: string; category: string | null; updated_at: string };
type PageLinks = { outbound: Page[]; backlinks: Page[] };
type Citation = { page_id: string; chunk_id: string; title: string; excerpt: string; source_location?: string };
type AskResult = { answer: string; evidence: string; citations: Citation[] };

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

// Notes are full of external references (X/Twitter posts, articles, local
// project links) that may be dead, moved, or unreachable. Opening them in
// the same tab replaces the whole app with the browser's own failure page
// the moment one doesn't resolve — open in a new tab instead so a bad link
// never costs the reader their place in the wiki.
const markdownComponents = {
  a: (props: ComponentPropsWithoutRef<"a">) => <a {...props} target="_blank" rel="noopener noreferrer" />,
};

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

  const categories = [...new Set(pages.map((page) => page.category).filter((category): category is string => Boolean(category)))].sort();
  const visiblePages = categoryFilter === null ? pages : pages.filter((page) => page.category === categoryFilter);
  const connectionsPage = pages.find((page) => page.category === "connections");

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
              <div className="markdown"><ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>{selectedPage.content}</ReactMarkdown></div>
              {(links.outbound.length > 0 || links.backlinks.length > 0) && <div className="connections">
                <div><b>Links to</b>{links.outbound.map((page) => <button key={page.id} onClick={() => openPage(page.id)}>{page.title}</button>)}</div>
                <div><b>Linked from</b>{links.backlinks.map((page) => <button key={page.id} onClick={() => openPage(page.id)}>{page.title}</button>)}</div>
              </div>}
            </section>}
          </div>

          <aside className="directory library">
            <div className="sectionTitle"><p className="eyebrow">LIBRARY</p><span>{pages.length} pages</span></div>
            {connectionsPage && <button className="connectionsLink" onClick={() => openPage(connectionsPage.id)}>🔗 跨文章关联索引 · {connectionsPage.title}</button>}
            {categories.length > 0 && <div className="categoryChips">
              <button className={categoryFilter === null ? "active" : ""} onClick={() => setCategoryFilter(null)}>All</button>
              {categories.map((category) => <button key={category} className={categoryFilter === category ? "active" : ""} onClick={() => setCategoryFilter(category)}>{category}</button>)}
            </div>}
            {visiblePages.length === 0 ? <p className="empty">Import a document or create a page to begin.</p> : <div className="directoryList">{visiblePages.map((page) => <div key={page.id} className="directoryItem">
              <button className="directoryOpen" title={page.title} onClick={() => openPage(page.id)}>
                <span className="directoryTitle">{page.title}</span>
                {page.category && <span className="categoryBadge">{page.category}</span>}
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
