# Layout Components

## RootLayout
Source: `frontend/app/layout.tsx`

```tsx
import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Proofpath — Work that speaks for itself",
  description: "Build a professional profile around evidence of the work you have done.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
```

## Current screen wrapper
Source: `frontend/app/[[...segments]]/page.tsx`

```tsx
import { LegacyScreen } from "../components/legacy-screen";
import { StatusPanel } from "../components/ui";

const staticRoutes: Record<string, string> = {
  "/": "/",
  "/signup": "/signup",
  "/login": "/login",
  "/security/sessions": "/security/sessions",
  "/verify-email": "/verify-email",
  "/candidate/onboarding": "/candidate/onboarding",
  "/candidate/dashboard": "/candidate/dashboard",
  "/projects": "/projects",
  "/jobs": "/jobs",
  "/applications": "/applications",
  "/organization/applications": "/organization/applications",
  "/organization/jobs": "/organization/jobs",
  "/organization/assessments": "/organization/assessments",
  "/organization/onboarding": "/organization/onboarding",
  "/organization/invitation": "/organization/invitation",
  "/reset-password": "/reset-password",
};

function legacyPath(segments: string[] = []) {
  const pathname = segments.length ? `/${segments.join("/")}` : "/";
  const direct = staticRoutes[pathname];
  if (direct) return direct;
  if (segments.length === 2 && segments[0] === "jobs") return pathname;
  if (segments.length === 2 && segments[0] === "portfolio") return pathname;
  return null;
}

export default async function Page({ params }: { params: Promise<{ segments?: string[] }> }) {
  const { segments = [] } = await params;
  const source = legacyPath(segments);

  if (!source) {
    return <StatusPanel title="Page not found" message="That Proofpath page does not exist." action="Go to home" href="/" />;
  }

  const encodedSource = source === "/" ? "" : `/${source.slice(1).split("/").map(encodeURIComponent).join("/")}`;
  return <LegacyScreen legacyPath={`/__legacy${encodedSource}`} />;
}
```

There is no shared navigation component yet. Each existing HTML page owns its own header, which is one source of visual inconsistency.
