import { LegacyScreen } from "../components/legacy-screen";
import { HomePage } from "../components/home-page";
import { Workspace, type Section } from "../components/workspace";
import { AuthPage } from "../components/auth-page";
import { OnboardingPage } from "../components/onboarding-page";
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
  "/projects": "/projects",
  "/app/portfolio/builder": "/app/portfolio/builder",
  "/reset-password": "/reset-password",
  "/manage/projects": "/projects",
};

function legacyPath(segments: string[] = []) {
  const pathname = segments.length ? `/${segments.join("/")}` : "/";
  const direct = staticRoutes[pathname];
  if (direct) return direct;
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
  if (pathname === "/login" || pathname === "/signup")
    return <AuthPage mode={pathname === "/login" ? "login" : "signup"} />;

  const workspaceRoutes: Record<string, Section> = {
    "/projects": "projects",
    "/profile": "profile",
    "/skills": "skills",
    "/resume": "resume",
    "/settings": "settings",
  };
  if (workspaceRoutes[pathname])
    return <Workspace key={pathname} section={workspaceRoutes[pathname]} />;
  if (segments[0] === "explore" && segments.length <= 2) {
    const section = segments[1] || "profile";
    if (["projects", "skills", "profile", "resume", "settings"].includes(section))
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
  const navigation = ["/login", "/signup", "/verify-email", "/reset-password"].includes(
          pathname,
        )
      ? undefined
      : pathname.startsWith("/portfolio/") ||
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
    profile: "Your profile",
    projects: "Your projects",
    skills: "Skills & evidence",
    resume: "Resume",
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
