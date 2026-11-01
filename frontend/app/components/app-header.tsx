import Link from "next/link";
import { BrandMark, Icon } from "./icons";

type Section = "public" | "candidate" | "employer";

const navigation: Record<Section, Array<{ label: string; href: string }>> = {
  public: [
    { label: "How it works", href: "/#how-it-works" },
    { label: "For talent", href: "/#candidates" },
    { label: "For teams", href: "/#teams" },
    { label: "Opportunities", href: "/jobs" },
  ],
  candidate: [
    { label: "Overview", href: "/candidate/dashboard" },
    { label: "Projects", href: "/projects" },
    { label: "Portfolio", href: "/app/portfolio/builder" },
    { label: "Find jobs", href: "/jobs" },
    { label: "Applications", href: "/applications" },
  ],
  employer: [
    { label: "Candidates", href: "/organization/candidates" },
    { label: "Jobs", href: "/organization/jobs" },
    { label: "Applications", href: "/organization/applications" },
    { label: "Assessments", href: "/organization/assessments" },
  ],
};

export function AppHeader({
  section,
  activePath,
}: {
  section: Section;
  activePath: string;
}) {
  const items = navigation[section];
  const brandLabel = "Proofpath home";

  return (
    <header className="app-header">
      <div className="app-header-inner">
        <Link className="app-brand" href="/" aria-label={brandLabel}>
          <BrandMark />
          <span>
            proofpath<span className="brand-period">.</span>
          </span>
        </Link>
        <nav className="app-nav" aria-label="Primary">
          {items.map((item) => (
            <Link
              key={item.href}
              href={item.href}
              aria-current={activePath === item.href ? "page" : undefined}
            >
              {item.label}
            </Link>
          ))}
        </nav>
        <div className="app-header-actions">
          <Link className="header-signin" href="/login">
            Sign in
          </Link>
          <Link
            className="header-cta"
            href={
              section === "employer"
                ? "/organization/onboarding"
                : "/signup?purpose=candidate"
            }
          >
            {section === "employer"
              ? "Workspace"
              : section === "candidate"
                ? "My profile"
                : "Get started"}{" "}
            <Icon name="arrow" size={16} />
          </Link>
        </div>
        <details className="app-mobile-menu">
          <summary aria-label="Open navigation">
            <Icon name="menu" />
          </summary>
          <nav aria-label="Mobile primary">
            {items.map((item) => (
              <Link key={item.href} href={item.href}>
                {item.label}
              </Link>
            ))}
            <Link href="/login">Sign in</Link>
          </nav>
        </details>
      </div>
    </header>
  );
}
