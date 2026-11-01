# Shared React UI Components

Framework: React 19 with Next.js App Router. Components are custom; there is no external component library.

## Button and StatusPanel
Source: `frontend/app/components/ui.tsx`

```tsx
"use client";

import type { ReactNode } from "react";

export function Button({ children, onClick, href }: { children: ReactNode; onClick?: () => void; href?: string }) {
  if (href) return <a className="ui-button" href={href}>{children}</a>;
  return (
    <button className="ui-button" type="button" onClick={onClick}>
      {children}
    </button>
  );
}

export function StatusPanel({
  title,
  message,
  action,
  href,
  onAction,
  loading = false,
}: {
  title: string;
  message: string;
  action?: string;
  href?: string;
  onAction?: () => void;
  loading?: boolean;
}) {
  return (
    <main className="status-page">
      <section className="status-panel" aria-live="polite" aria-busy={loading}>
        <a className="ui-brand" href="/" aria-label="Proofpath home">Proofpath<span>.</span></a>
        <div className="status-content">
          {loading && <span className="status-mark" aria-hidden="true" />}
          <h1>{title}</h1>
          <p>{message}</p>
          {action && <Button href={href} onClick={onAction}>{action}</Button>}
        </div>
      </section>
    </main>
  );
}
```

## LegacyScreen
Source: `frontend/app/components/legacy-screen.tsx`

```tsx
"use client";

import { useEffect, useState } from "react";
import { StatusPanel } from "./ui";

type PagePayload = {
  body: string;
  bodyClass: string;
  scripts: Array<{ source: string; code: string; type: string }>;
};

export function LegacyScreen({ legacyPath }: { legacyPath: string }) {
  const [page, setPage] = useState<PagePayload | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    const headNodes: HTMLElement[] = [];

    async function loadPage() {
      try {
        const response = await fetch(`${legacyPath}${window.location.search}`, {
          cache: "no-store",
          credentials: "same-origin",
          signal: controller.signal,
        });
        const source = await response.text();
        if (!response.ok) throw new Error(`This page could not be loaded (${response.status}).`);

        const parsed = new DOMParser().parseFromString(source, "text/html");
        document.title = parsed.title || "Proofpath";

        for (const node of parsed.head.querySelectorAll('link[rel="stylesheet"], link[rel="preconnect"], meta[name="referrer"], style')) {
          const copy = node.cloneNode(true) as HTMLElement;
          copy.setAttribute("data-legacy-head", "true");
          document.head.append(copy);
          headNodes.push(copy);
        }

        setPage({
          body: parsed.body.innerHTML,
          bodyClass: parsed.body.className,
          scripts: Array.from(parsed.querySelectorAll("script")).map((script) => ({
            source: script.getAttribute("src") || "",
            code: script.textContent || "",
            type: script.type,
          })),
        });
      } catch (reason) {
        if (!controller.signal.aborted) {
          setError(reason instanceof Error ? reason.message : "Could not reach Proofpath. Try again.");
        }
      }
    }

    void loadPage();
    return () => {
      controller.abort();
      headNodes.forEach((node) => node.remove());
    };
  }, [legacyPath]);

  useEffect(() => {
    if (!page) return;
    const scripts = page.scripts;
    let cancelled = false;
    const injectedScripts: HTMLScriptElement[] = [];

    async function runPageScripts() {
      for (const item of scripts) {
        if (cancelled) return;
        const script = document.createElement("script");
        if (item.type) script.type = item.type;
        if (item.source) {
          script.src = new URL(item.source, window.location.href).href;
          script.async = false;
          document.body.append(script);
          injectedScripts.push(script);
          await new Promise<void>((resolve) => {
            script.addEventListener("load", () => resolve(), { once: true });
            script.addEventListener("error", () => resolve(), { once: true });
          });
        } else {
          script.textContent = item.code;
          document.body.append(script);
          injectedScripts.push(script);
        }
      }
    }

    void runPageScripts();
    return () => {
      cancelled = true;
      injectedScripts.forEach((script) => script.remove());
    };
  }, [page]);

  if (error) return <StatusPanel title="We couldn’t open this page" message={error} action="Try again" onAction={() => window.location.reload()} />;
  if (!page) return <StatusPanel title="Loading Proofpath" message="Opening your page…" loading />;

  // shortcut: screens still use trusted HTML and DOM scripts; port each flow when its UI is rebuilt in React.
  return (
    <div
      className={`legacy-screen ${page.bodyClass}`.trim()}
      dangerouslySetInnerHTML={{ __html: page.body }}
    />
  );
}
```
