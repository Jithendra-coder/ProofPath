"use client";

import type { ReactNode } from "react";
import Link from "next/link";

export function Button({
  children,
  onClick,
  href,
  variant = "primary",
}: {
  children: ReactNode;
  onClick?: () => void;
  href?: string;
  variant?: "primary" | "secondary";
}) {
  const className = `ui-button ui-button-${variant}`;
  if (href)
    return (
      <Link className={className} href={href}>
        {children}
      </Link>
    );
  return (
    <button className={className} type="button" onClick={onClick}>
      {children}
    </button>
  );
}

export function Badge({
  children,
  tone = "default",
}: {
  children: ReactNode;
  tone?: "default" | "good" | "notice" | "accent";
}) {
  return <span className={`ui-badge ui-badge-${tone}`}>{children}</span>;
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
    <main id="main-content" className="status-page">
      <section className="status-panel" aria-live="polite" aria-busy={loading}>
        <a className="ui-brand" href="/" aria-label="Proofpath home">
          Proofpath<span>.</span>
        </a>
        <div className="status-content">
          {loading && <span className="status-mark" aria-hidden="true" />}
          <h1>{title}</h1>
          <p>{message}</p>
          {action && (
            <Button href={href} onClick={onAction}>
              {action}
            </Button>
          )}
        </div>
      </section>
    </main>
  );
}
