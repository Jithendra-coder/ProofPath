import { LegacyScreen } from "../components/legacy-screen";
import { HomePage } from "../components/home-page";
import { Workspace, type Section } from "../components/workspace";
import { AuthPage } from "../components/auth-page";
import { OnboardingPage } from "../components/onboarding-page";
import { JobPage } from "../components/job-page";
import { notFound } from "next/navigation";
import type { Metadata } from "next";

const staticRoutes: Record<string, string> = {
  "/": "/",
  "/signup": "/signup",
  "/login": "/login",
  "/security/sessions": "/security/sessions",
  "/account/privacy": "/account/privacy",
  "/verify-email": "/verify-email",
  "/candidate/onboarding": "/candidate/onboarding",
  "/candidate/dashboard": "/candidate/dashboard",
  "/projects": "/projects",
  "/app/portfolio/builder": "/app/portfolio/builder",
  "/jobs": "/jobs",
  "/applications": "/applications",
  "/organization/applications": "/organization/applications",
  "/organization/jobs": "/organization/jobs",
  "/organization/assessments": "/organization/assessments",
  "/organization/candidates": "/organization/candidates",
  "/organization/onboarding": "/organization/onboarding",
  "/organization/invitation": "/organization/invitation",
  "/reset-password": "/reset-password",
  "/assessment-workspace": "/assessment-workspace",
  "/manage/projects": "/projects",
  "/manage/applications": "/applications",
};

function legacyPath(segments: string[] = []) {
  const pathname = segments.length ? `/${segments.join("/")}` : "/";
  const direct = staticRoutes[pathname];
  if (direct) return direct;
  if (segments.length === 2 && segments[0] === "jobs") return pathname;
  if (
    segments.length === 2 &&
    (segments[0] === "portfolio" || segments[0] === "u")
  )
    return pathname;
  return null;
}

export default async function Page({
  params,
}: {
  params: Promise<{ segments?: string[] }>;
}) {
  const { segments = [] } = await params;
  const pathname = segments.length ? `/${segments.join("/")}` : "/";

  if (pathname === "/") return <HomePage />;
  if (pathname === "/candidate/onboarding") return <OnboardingPage />;
  if (segments.length === 2 && segments[0] === "jobs")
    return <JobPage jobId={segments[1]} />;
  if (pathname === "/login" || pathname === "/signup")
    return <AuthPage mode={pathname === "/login" ? "login" : "signup"} />;

  const workspaceRoutes: Record<string, Section> = {
    "/candidate/dashboard": "overview",
    "/projects": "projects",
    "/jobs": "opportunities",
    "/profile": "profile",
    "/skills": "skills",
    "/applications": "applications",
    "/settings": "settings",
  };
  if (workspaceRoutes[pathname])
    return <Workspace key={pathname} section={workspaceRoutes[pathname]} />;
  if (segments[0] === "explore" && segments.length <= 2) {
    const section = segments[1] || "overview";
    if (
      [
        "overview",
        "projects",
        "skills",
        "opportunities",
        "applications",
        "profile",
        "settings",
      ].includes(section)
    )
      return <Workspace key={pathname} section={section as Section} demo />;
  }

  const source = legacyPath(segments);

  if (!source) {
    notFound();
  }

  const encodedSource =
    source === "/"
      ? ""
      : `/${source.slice(1).split("/").map(encodeURIComponent).join("/")}`;
  const navigation = pathname.startsWith("/organization/")
    ? "employer"
    : ["/login", "/signup", "/verify-email", "/reset-password"].includes(
          pathname,
        )
      ? undefined
      : pathname === "/jobs" ||
          pathname.startsWith("/jobs/") ||
          pathname.startsWith("/portfolio/") ||
          pathname.startsWith("/u/")
        ? "public"
        : "candidate";
  return (
    <LegacyScreen
      key={pathname}
      legacyPath={`/__legacy${encodedSource}`}
      navigation={navigation}
      activePath={pathname}
    />
  );
}

export async function generateMetadata({
  params,
}: {
  params: Promise<{ segments?: string[] }>;
}): Promise<Metadata> {
  const { segments = [] } = await params;
  const names: Record<string, string> = {
    explore: "Your workspace",
    dashboard: "Your workspace",
    profile: "Your profile",
    projects: "Your projects",
    skills: "Skills & evidence",
    opportunities: "Opportunities",
    jobs: "Opportunities",
    applications: "Your applications",
    signup: "Start your next chapter",
    login: "Welcome back",
    onboarding: "Make your introduction",
    settings: "Your settings",
  };
  const name = names[segments[segments.length - 1]];
  return {
    title: name
      ? `${name} — Proofpath`
      : "Proofpath — Let your work lead the way",
  };
}
