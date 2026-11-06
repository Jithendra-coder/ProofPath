"use client";

import { useEffect, useState } from "react";
import { AppHeader } from "./app-header";
import { StatusPanel } from "./ui";

type NavigationSection = "public" | "candidate";

type PagePayload = {
  body: string;
  bodyClass: string;
  scripts: Array<{ source: string; code: string; type: string }>;
};

export function LegacyScreen({
  legacyPath,
  navigation,
  activePath,
}: {
  legacyPath: string;
  navigation?: NavigationSection;
  activePath: string;
}) {
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
        if (!response.ok)
          throw new Error(
            `This page could not be loaded (${response.status}).`,
          );

        const parsed = new DOMParser().parseFromString(source, "text/html");
        parsed.querySelector("main")?.setAttribute("id", "main-content");
        document.title = parsed.title || "Proofpath";

        for (const node of parsed.head.querySelectorAll(
          'link[rel="stylesheet"], link[rel="preconnect"], meta[name="referrer"], style',
        )) {
          const copy = node.cloneNode(true) as HTMLElement;
          copy.setAttribute("data-legacy-head", "true");
          document.head.append(copy);
          headNodes.push(copy);
        }

        setPage({
          body: parsed.body.innerHTML,
          bodyClass: parsed.body.className,
          scripts: Array.from(parsed.querySelectorAll("script")).map(
            (script) => ({
              source: script.getAttribute("src") || "",
              code: script.textContent || "",
              type: script.type,
            }),
          ),
        });
      } catch (reason) {
        if (!controller.signal.aborted) {
          setError(
            reason instanceof Error
              ? reason.message
              : "Could not reach Proofpath. Try again.",
          );
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
          // Keep page-local declarations isolated when Next navigates between legacy forms.
          script.textContent =
            item.type === "module"
              ? item.code
              : `(() => {\n${item.code}\n})();`;
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

  if (error)
    return (
      <StatusPanel
        title="We couldn’t open this page"
        message={error}
        action="Try again"
        onAction={() => window.location.reload()}
      />
    );
  if (!page)
    return (
      <StatusPanel
        title="Loading Proofpath"
        message="Opening your page…"
        loading
      />
    );

  // shortcut: screens still use trusted HTML and DOM scripts; port each flow when its UI is rebuilt in React.
  return (
    <>
      {navigation && <AppHeader section={navigation} activePath={activePath} />}
      <div
        className={`legacy-screen ${navigation ? "legacy-screen--shell" : ""} ${page.bodyClass}`.trim()}
        dangerouslySetInnerHTML={{ __html: page.body }}
      />
    </>
  );
}
