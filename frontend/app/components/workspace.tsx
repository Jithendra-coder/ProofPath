"use client";

import Link from "next/link";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { api, messageOf } from "../lib/api";
import {
  emptyData,
  exampleData,
  parseDemoState,
  type Profile,
  type Project,
  type ResumeSnapshot,
  type WorkspaceData,
} from "../lib/workspace-data";
import { BrandMark, Icon, type IconName } from "./icons";
import { Dialog } from "./dialog";
import {
  ProjectsView,
  ProfileView,
  SkillsView,
  SettingsView,
} from "./workspace-views";
import { ResumeView } from "./resume-view";

export type Section =
  | "projects"
  | "skills"
  | "profile"
  | "resume"
  | "settings";
const nav: { key: Section; label: string; icon: IconName; path: string }[] = [
  { key: "profile", label: "My profile", icon: "user", path: "/profile" },
  { key: "projects", label: "My projects", icon: "folder", path: "/projects" },
  { key: "skills", label: "Skills & evidence", icon: "award", path: "/skills" },
  { key: "resume", label: "Resume", icon: "folder", path: "/resume" },
];
const storageKey = "proofpath-example-v1";

export type ViewProps = {
  data: WorkspaceData;
  demo: boolean;
  href: (section: Section) => string;
  onProject: (project?: Project) => void;
  onProfile: () => void;
  onResume: (resume: ResumeSnapshot | null) => void;
  onApplyResume: (skills: string[], roles: string[], name?: string) => Promise<void>;
  notify: (message: string) => void;
};

export function Workspace({
  section,
  demo = false,
}: {
  section: Section;
  demo?: boolean;
}) {
  const [data, setData] = useState<WorkspaceData>(() =>
    structuredClone(demo ? exampleData : emptyData),
  );
  const [loading, setLoading] = useState(!demo);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState("");
  const [toast, setToast] = useState("");
  const [menu, setMenu] = useState(false);
  const [mobile, setMobile] = useState(false);
  const sidebar = useRef<HTMLElement>(null);
  const [editor, setEditor] = useState<Project | "new" | null>(null);
  const [profileOpen, setProfileOpen] = useState(false);
  const [resetOpen, setResetOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState("");
  const href = (key: Section) =>
    demo
      ? `/explore/${key}`
      : key === "settings"
        ? "/settings"
        : nav.find((item) => item.key === key)!.path;

  useEffect(() => {
    if (demo) {
      try {
        setData(parseDemoState(localStorage.getItem(storageKey)));
      } catch {
        setToast(
          "Browser storage is unavailable. Changes will last until you leave this page.",
        );
      }
      setReady(true);
      return;
    }
    const controller = new AbortController();
    async function load() {
      setError("");
      setLoading(true);
      try {
        const [profile, portfolio, resume] = await Promise.all([
              api<Profile>(
                "/me/candidate/onboarding",
                "GET",
                undefined,
                controller.signal,
              ),
              api<{
                projects: Project[];
                public_handle: string | null;
                published_at: string | null;
              }>("/me/portfolio", "GET", undefined, controller.signal),
              api<{ resume: ResumeSnapshot | null }>("/me/resume", "GET", undefined, controller.signal),
            ]);
        setData({
          profile,
          projects: portfolio.projects,
          resume: resume.resume,
          public_handle: portfolio.public_handle,
          published_at: portfolio.published_at,
        });
      } catch (reason) {
        if (!controller.signal.aborted) setError(messageOf(reason));
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    }
    void load();
    return () => controller.abort();
  }, [demo, section]);

  useEffect(() => {
    if (!demo || !ready) return;
    // shortcut: example activity is browser-local; real accounts use the authenticated API.
    try {
      localStorage.setItem(storageKey, JSON.stringify(data));
    } catch {
      setToast(
        "Your changes could not be saved in this browser. Keep this page open to retain them.",
      );
    }
  }, [data, demo, ready]);
  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(""), 6500);
    return () => clearTimeout(timer);
  }, [toast]);
  useEffect(() => {
    const media = window.matchMedia("(max-width: 760px)");
    const update = () => {
      setMobile(media.matches);
      if (!media.matches) setMenu(false);
    };
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);
  useEffect(() => {
    if (!menu || !mobile) return;
    const previous = document.activeElement as HTMLElement | null;
    const links =
      sidebar.current?.querySelectorAll<HTMLAnchorElement>("a[href]");
    links?.[0]?.focus();
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMenu(false);
      if (event.key === "Tab" && links?.length) {
        if (event.shiftKey && document.activeElement === links[0]) {
          event.preventDefault();
          links[links.length - 1].focus();
        } else if (
          !event.shiftKey &&
          document.activeElement === links[links.length - 1]
        ) {
          event.preventDefault();
          links[0].focus();
        }
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = overflow;
      previous?.focus();
    };
  }, [menu, mobile]);

  async function saveProject(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setFormError("");
    const values = new FormData(event.currentTarget);
    const body = {
      name: String(values.get("name")).trim(),
      problem_statement: String(values.get("problem_statement")).trim(),
      contribution: String(values.get("contribution")).trim(),
      technologies: [
        ...new Set(
          String(values.get("technologies"))
            .split(",")
            .map((value) => value.trim())
            .filter(Boolean),
        ),
      ],
    };
    try {
      if (body.name.length < 2)
        throw new Error("Give your project a name with at least 2 characters.");
      if (
        body.technologies.length > 12 ||
        body.technologies.some((value) => value.length > 40)
      )
        throw new Error(
          "Use up to 12 skills, with no more than 40 characters each.",
        );
      const existing = editor && editor !== "new" ? editor : null;
      const project: Project = demo
        ? {
            ...existing,
            ...body,
            id: existing?.id || crypto.randomUUID(),
            visibility:
              values.get("visibility") === "public" ? "public" : "private",
            version: (existing?.version || 0) + 1,
            artwork: existing?.artwork || "custom",
          }
        : await api<Project>(
            existing
              ? `/me/projects/${encodeURIComponent(existing.id)}`
              : "/me/projects",
            existing ? "PATCH" : "POST",
            {
              ...body,
              ...(existing
                ? { expected_version: existing.version }
                : { request_key: crypto.randomUUID() }),
            },
          );
      setData((previous) => ({
        ...previous,
        projects: existing
          ? previous.projects.map((item) =>
              item.id === project.id ? project : item,
            )
          : [project, ...previous.projects],
      }));
      setEditor(null);
      setToast(
        existing
          ? "Project changes saved."
          : "Project saved as a private draft. Your story is taking shape.",
      );
    } catch (reason) {
      setFormError(messageOf(reason));
    } finally {
      setBusy(false);
    }
  }

  function deleteExampleProject(project: Project) {
    if (
      !demo ||
      !window.confirm(`Delete “${project.name}” from this example workspace?`)
    )
      return;
    setData((previous) => ({
      ...previous,
      projects: previous.projects.filter((item) => item.id !== project.id),
    }));
    setEditor(null);
    setToast("Example project deleted.");
  }

  async function saveProfile(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setFormError("");
    const values = new FormData(event.currentTarget);
    const profile: Profile = {
      ...data.profile,
      full_name: String(values.get("full_name")).trim(),
      target_roles: String(values.get("target_roles"))
        .split(",")
        .map((value) => value.trim())
        .filter(Boolean),
      skills: [
        ...new Set(
          String(values.get("skills"))
            .split(",")
            .map((value) => value.trim())
            .filter(Boolean),
        ),
      ],
    };
    try {
      if (!profile.full_name) throw new Error("Enter your name.");
      if (profile.skills.length > 20 || profile.target_roles.length > 5)
        throw new Error("Add up to 20 skills and 5 target roles.");
      const result = demo
        ? profile
        : await api<Profile>("/me/candidate/onboarding", "PATCH", {
            full_name: profile.full_name,
            target_roles: profile.target_roles,
            skills: profile.skills,
            expected_version: profile.version,
          });
      setData((previous) => ({ ...previous, profile: result }));
      setProfileOpen(false);
      setToast("Your profile has been updated.");
    } catch (reason) {
      setFormError(messageOf(reason));
    } finally {
      setBusy(false);
    }
  }

  const openProject = (project?: Project) => {
    setFormError("");
    setEditor(project || "new");
  };
  const openProfile = () => {
    setFormError("");
    setProfileOpen(true);
  };
  async function applyResume(skills: string[], roles: string[], name?: string) {
    setBusy(true);
    try {
      const unique = new Map<string, string>();
      [...data.profile.skills, ...skills].forEach((skill) => unique.set(skill.toLocaleLowerCase(), skill));
      const uniqueRoles = new Map<string, string>();
      [...data.profile.target_roles, ...roles].forEach((role) => uniqueRoles.set(role.toLocaleLowerCase(), role));
      if (unique.size > 20 || uniqueRoles.size > 5) throw new Error("Your profile supports up to 20 skills and 5 target roles. Remove a profile item before adding more.");
      const profile: Profile = { ...data.profile, ...(name ? { full_name: name } : {}), skills: [...unique.values()], target_roles: [...uniqueRoles.values()] };
      const result = demo ? profile : await api<Profile>("/me/candidate/onboarding", "PATCH", {
        full_name: profile.full_name,
        target_roles: profile.target_roles,
        skills: profile.skills,
        expected_version: data.profile.version,
      });
      setData((previous) => ({ ...previous, profile: result }));
      setToast("Selected resume details added to your profile.");
    } catch (reason) { setToast(messageOf(reason)); }
    finally { setBusy(false); }
  }
  const updateResume = (resume: ResumeSnapshot | null) => setData((previous) => ({ ...previous, resume }));
  const viewProps: ViewProps = {
    data,
    demo,
    href,
    onProject: openProject,
    onProfile: openProfile,
    onResume: updateResume,
    onApplyResume: applyResume,
    notify: setToast,
  };
  const initials =
    data.profile.full_name
      .split(" ")
      .map((part) => part[0])
      .slice(0, 2)
      .join("")
      .toLowerCase() || "you";
  const currentTitle =
    section === "settings"
      ? "Settings"
      : nav.find((item) => item.key === section)?.label;

  return (
    <div className="workspace">
      {menu && (
        <button
          className="sidebar-scrim"
          aria-label="Close navigation"
          onClick={() => setMenu(false)}
        />
      )}
      <aside
        ref={sidebar}
        inert={mobile && !menu}
        className={`workspace-sidebar ${menu ? "sidebar-open" : ""}`}
      >
        <Link href="/" className="app-brand sidebar-brand">
          <BrandMark />
          <span>
            proofpath<span className="brand-period">.</span>
          </span>
        </Link>
        <div className="workspace-label">
          <span>YOUR WORKSPACE</span>
          <span className="workspace-label-dot" />
        </div>
        <nav aria-label="Workspace navigation">
          {nav.map((item, index) => (
            <div key={item.key} className={index === 4 ? "nav-group" : ""}>
              {index === 4 && (
                <span className="nav-section-label">YOUR NEXT CHAPTER</span>
              )}
              <Link
                href={href(item.key)}
                aria-current={section === item.key ? "page" : undefined}
                onClick={() => setMenu(false)}
              >
                <Icon name={item.icon} size={19} />
                <span>{item.label}</span>
              </Link>
            </div>
          ))}
          {!demo && (
            <Link href="/app/portfolio/builder" onClick={() => setMenu(false)}>
              <Icon name="briefcase" size={19} />
              <span>My portfolio</span>
            </Link>
          )}
        </nav>
        <div className="sidebar-bottom">
          <Link
            className="sidebar-utility"
            href={href("settings")}
            aria-current={section === "settings" ? "page" : undefined}
          >
            <Icon name="settings" size={18} /> Settings
          </Link>
          <Link className="sidebar-utility" href="/#faq">
            <Icon name="help" size={18} /> Help & resources
          </Link>
          <Link href={href("profile")} className="sidebar-account">
            <span className="avatar">{initials}</span>
            <span>
              <b>{data.profile.full_name || "Your workspace"}</b>
              <small>{demo ? "Example profile" : "Personal account"}</small>
            </span>
            <Icon name="down" size={16} />
          </Link>
        </div>
      </aside>
      <div className="workspace-body" inert={mobile && menu}>
        <header className="workspace-topbar">
          <div className="topbar-breadcrumb">
            <button
              className="icon-button mobile-nav-toggle"
              aria-label="Open navigation"
              aria-expanded={menu}
              onClick={() => setMenu(true)}
            >
              <Icon name="menu" />
            </button>
            <span>Workspace</span>
            <Icon name="chevron" size={13} />
            <strong>{currentTitle}</strong>
          </div>
          <div className="topbar-actions">
            <Link
              href={href("profile")}
              aria-label="Open your profile"
              className="avatar avatar-small"
            >
              {initials}
            </Link>
          </div>
        </header>
        {demo && (
          <div className="demo-banner">
            <span>
              <Icon name="sparkle" size={14} />
              <b>Make yourself at home.</b> You’re exploring an example
              workspace.
            </span>
            <Link href="/signup">
              Create your own <Icon name="arrow" size={14} />
            </Link>
          </div>
        )}
        <main id="main-content" className="workspace-main">
          {loading ? (
            <div
              className="loading-workspace"
              role="status"
              aria-label="Loading your workspace"
            >
              <span className="eyebrow">GETTING THINGS READY</span>
              <h1>Your next chapter is loading.</h1>
              <div className="skeleton skeleton-wide" />
              <div className="skeleton-grid">
                <div className="skeleton" />
                <div className="skeleton" />
                <div className="skeleton" />
              </div>
            </div>
          ) : error ? (
            <div className="workspace-error">
              <span className="empty-icon">
                <Icon name="lock" size={28} />
              </span>
              <h1>Let’s get you to your workspace.</h1>
              <p>{error}</p>
              <div className="button-row">
                <Link className="button primary" href="/login">
                  Sign in <Icon name="arrow" size={17} />
                </Link>
                <Link className="button secondary" href="/explore">
                  Explore an example
                </Link>
                <button
                  className="button plain"
                  onClick={() => window.location.reload()}
                >
                  Try again
                </button>
              </div>
            </div>
          ) : (
            <>
              {section === "projects" && <ProjectsView {...viewProps} />}
              {section === "profile" && <ProfileView {...viewProps} />}
              {section === "skills" && <SkillsView {...viewProps} />}
              {section === "resume" && <ResumeView {...viewProps} />}
              {section === "settings" && (
                <SettingsView
                  {...viewProps}
                  onReset={() => setResetOpen(true)}
                />
              )}
            </>
          )}
        </main>
        <footer className="workspace-footer">
          <span>A little more you. A little more possibility.</span>
          <span>
            Proofpath © 2026{" "}
            <span className="footer-detail">· Built around ability</span>
          </span>
        </footer>
      </div>
      {toast && (
        <div className="toast" role="status">
          <Icon name="check" size={18} />
          <span>{toast}</span>
          <button
            onClick={() => setToast("")}
            aria-label="Dismiss notification"
          >
            <Icon name="close" size={16} />
          </button>
        </div>
      )}
      {editor && (
        <Dialog
          title={
            editor === "new"
              ? "Add your next piece of proof"
              : "Edit your project"
          }
          onClose={() => {
            if (!busy) setEditor(null);
          }}
        >
          <form className="editor-form" onSubmit={saveProject}>
            <p className="muted">
              Tell the story behind the work. New projects start as private
              drafts.
            </p>
            <label>
              Project name
              <input
                autoFocus
                name="name"
                required
                minLength={2}
                maxLength={160}
                defaultValue={editor === "new" ? "" : editor.name}
                placeholder="Give your work a name"
              />
            </label>
            <label>
              The problem
              <textarea
                name="problem_statement"
                maxLength={2000}
                rows={3}
                defaultValue={editor === "new" ? "" : editor.problem_statement}
                placeholder="What challenge were you trying to solve?"
              />
            </label>
            <label>
              Your contribution
              <textarea
                name="contribution"
                maxLength={4000}
                rows={4}
                defaultValue={editor === "new" ? "" : editor.contribution}
                placeholder="What did you do, and what did you learn?"
              />
            </label>
            <label>
              Skills & tools
              <input
                name="technologies"
                maxLength={500}
                defaultValue={
                  editor === "new" ? "" : editor.technologies.join(", ")
                }
                placeholder="e.g. Product design, Figma, React"
              />
              <small>Separate with commas. Add up to 12.</small>
            </label>
            {demo && (
              <label>
                Visibility
                <select
                  name="visibility"
                  defaultValue={
                    editor === "new" ? "private" : editor.visibility
                  }
                >
                  <option value="private">Private draft</option>
                  <option value="public">Show on example profile</option>
                </select>
                <small>
                  Example only. This does not publish anything outside this
                  browser.
                </small>
              </label>
            )}
            {formError && (
              <p role="alert" className="form-error">
                {formError}
              </p>
            )}
            <div className="dialog-footer">
              <span>
                <Icon name="lock" size={14} />{" "}
                {editor === "new"
                  ? "Private until you share"
                  : editor.visibility === "public"
                    ? "This project is public"
                    : "Private draft"}
              </span>
              <button
                type="button"
                className="button secondary"
                disabled={busy}
                onClick={() => setEditor(null)}
              >
                Cancel
              </button>
              {demo && editor !== "new" && (
                <button
                  type="button"
                  className="button plain"
                  disabled={busy}
                  onClick={() => deleteExampleProject(editor)}
                >
                  Delete
                </button>
              )}
              <button className="button primary" disabled={busy}>
                {busy ? "Saving…" : "Save project"}
                <Icon name="arrow" size={16} />
              </button>
            </div>
          </form>
        </Dialog>
      )}
      {profileOpen && (
        <Dialog
          title="Make your profile your own"
          onClose={() => {
            if (!busy) setProfileOpen(false);
          }}
        >
          <form className="editor-form" onSubmit={saveProfile}>
            <p className="muted">
              A few words about who you are and where you want to go.
            </p>
            {!demo && data.profile.status === "complete" ? (
              <>
                <p>
                  Your onboarding is complete. Manage your public profile and
                  sharing preferences in your portfolio controls.
                </p>
                <Link className="button primary" href="/manage/projects">
                  Open profile controls <Icon name="arrow" size={16} />
                </Link>
              </>
            ) : (
              <>
                <label>
                  Full name
                  <input
                    autoFocus
                    name="full_name"
                    required
                    maxLength={120}
                    defaultValue={data.profile.full_name}
                  />
                </label>
                <label>
                  Target roles
                  <input
                    name="target_roles"
                    maxLength={500}
                    defaultValue={data.profile.target_roles.join(", ")}
                    placeholder="Product Designer, Design Engineer"
                  />
                  <small>Up to five roles, separated by commas.</small>
                </label>
                <label>
                  Your skills
                  <textarea
                    name="skills"
                    maxLength={2000}
                    rows={3}
                    defaultValue={data.profile.skills.join(", ")}
                  />
                  <small>
                    Up to 20 skills, separated by commas. These are
                    self-reported.
                  </small>
                </label>
                {formError && (
                  <p className="form-error" role="alert">
                    {formError}
                  </p>
                )}
                <div className="dialog-footer">
                  <button
                    className="button secondary"
                    type="button"
                    onClick={() => setProfileOpen(false)}
                    disabled={busy}
                  >
                    Cancel
                  </button>
                  <button className="button primary" disabled={busy}>
                    {busy ? "Saving…" : "Save profile"}
                  </button>
                </div>
              </>
            )}
          </form>
        </Dialog>
      )}
      {resetOpen && (
        <Dialog
          title="Start the example fresh?"
          onClose={() => setResetOpen(false)}
        >
          <div className="editor-form">
            <p>
              This removes the projects and profile edits you made in this
              browser. Your real account is unaffected.
            </p>
            <div className="dialog-footer">
              <button
                className="button secondary"
                onClick={() => setResetOpen(false)}
              >
                Keep my changes
              </button>
              <button
                className="button primary"
                onClick={() => {
                  setData(structuredClone(exampleData));
                  setResetOpen(false);
                  setToast("Example workspace reset.");
                }}
              >
                Reset example
              </button>
            </div>
          </div>
        </Dialog>
      )}
    </div>
  );
}
