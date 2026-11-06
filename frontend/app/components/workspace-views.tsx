"use client";

import Link from "next/link";
import { useEffect, useState, type ReactNode } from "react";
import {
  skillEvidence,
  type Project,
} from "../lib/workspace-data";
import { Icon, type IconName } from "./icons";
import { ProjectArt } from "./project-art";
import type { ViewProps } from "./workspace";

function PageHeading({
  eyebrow,
  title,
  text,
  children,
}: {
  eyebrow: string;
  title: string;
  text: string;
  children?: ReactNode;
}) {
  return (
    <div className="page-heading">
      <div>
        <div className="eyebrow">{eyebrow}</div>
        <h1>{title}</h1>
        <p>{text}</p>
      </div>
      {children && <div className="page-heading-actions">{children}</div>}
    </div>
  );
}

function EmptyState({
  icon = "search",
  title,
  text,
  children,
}: {
  icon?: IconName;
  title: string;
  text: string;
  children?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <span className="empty-icon">
        <Icon name={icon} size={27} />
      </span>
      <h3>{title}</h3>
      <p>{text}</p>
      {children}
    </div>
  );
}

function ProjectCard({
  project,
  onClick,
}: {
  project: Project;
  onClick: () => void;
}) {
  return (
    <article className="project-card">
      <button
        className="project-art-button"
        onClick={onClick}
        aria-label={`Edit ${project.name}`}
      >
        <ProjectArt kind={project.artwork || "custom"} />
        <span
          className={`project-status ${project.visibility === "private" ? "is-private" : ""}`}
        >
          <Icon
            name={project.visibility === "private" ? "lock" : "globe"}
            size={11}
          />
          {project.visibility === "public" ? "Public" : "Private draft"}
        </span>
        <span className="project-hover">
          <Icon name="upRight" />
        </span>
      </button>
      <div className="project-card-content">
        <button className="project-title" onClick={onClick}>
          {project.name}
          <Icon name="upRight" size={16} />
        </button>
        <p>
          {project.problem_statement || "Every project has a story. Add yours."}
        </p>
        <div className="skill-tags">
          {project.technologies.slice(0, 3).map((skill) => (
            <span key={skill}>{skill}</span>
          ))}
        </div>
      </div>
    </article>
  );
}

export function ProjectsView({ data, demo, onProject }: ViewProps) {
  const [tab, setTab] = useState("All projects");
  const [query, setQuery] = useState("");
  const projects = data.projects.filter(
    (project) =>
      (tab === "All projects" ||
        project.visibility === (tab === "Public" ? "public" : "private")) &&
      `${project.name} ${project.technologies.join(" ")}`
        .toLowerCase()
        .includes(query.toLowerCase().trim()),
  );
  return (
    <>
      <PageHeading
        eyebrow="YOUR WORK, YOUR STORY"
        title="Proof of what you bring."
        text="The ideas, the process, the things you made happen. All in one place."
      >
        <button className="button primary" onClick={() => onProject()}>
          <Icon name="plus" size={17} /> Add a project
        </button>
      </PageHeading>
      <div className="filter-bar">
        <div className="tabs" aria-label="Project visibility">
          {["All projects", "Public", "Private drafts"].map((item) => (
            <button
              key={item}
              aria-pressed={tab === item}
              onClick={() => setTab(item)}
            >
              {item}
              {item === "All projects" && <span>{data.projects.length}</span>}
            </button>
          ))}
        </div>
        <label className="search-field">
          <Icon name="search" size={17} />
          <input
            aria-label="Search projects"
            placeholder="Find a project…"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>
      </div>
      {projects.length ? (
        <div className="project-grid project-grid-full">
          {projects.map((project) => (
            <ProjectCard
              key={project.id}
              project={project}
              onClick={() => onProject(project)}
            />
          ))}
          <button className="add-project-card" onClick={() => onProject()}>
            <span>
              <Icon name="plus" size={27} />
            </span>
            <b>What have you been working on?</b>
            <p>Give your next project a home.</p>
          </button>
        </div>
      ) : (
        <EmptyState
          icon="folder"
          title={query ? "No projects found." : "Every story starts somewhere."}
          text={
            query
              ? "Try another project name or skill."
              : "Add your first project and show the thinking behind your work."
          }
        >
          <button className="button primary" onClick={() => onProject()}>
            Add a project
          </button>
        </EmptyState>
      )}
      <div className="inline-note">
        <Icon name="shield" />
        <p>
          New projects start private.{" "}
          {demo ? (
            "Changes in this example are saved only in your browser."
          ) : (
            <>
              <Link href="/manage/projects">
                Manage publishing, source links, and evidence
              </Link>{" "}
              when you’re ready to share.
            </>
          )}
        </p>
      </div>
    </>
  );
}

export function SkillsView({ data, onProfile, onProject, demo }: ViewProps) {
  const [query, setQuery] = useState("");
  const skills = data.profile.skills.filter((skill) =>
    skill.toLowerCase().includes(query.toLowerCase().trim()),
  );
  return (
    <>
      <PageHeading
        eyebrow="ABILITIES WITH A STORY"
        title="More than a list of skills."
        text="Connect what you know to what you’ve done. Let your work add the context."
      >
        <button className="button primary" onClick={onProfile}>
          <Icon name="plus" size={17} /> Add skills
        </button>
      </PageHeading>
      <div className="skills-explainer">
        <span className="soft-icon">
          <Icon name="award" size={27} />
        </span>
        <div>
          <h2>Claims are a starting point. Evidence goes further.</h2>
          <p>
            Skills you add are self-reported. Projects show how you use them.
            Assessment results are not available in this product version yet.
          </p>
        </div>
      </div>
      <div className="block-heading skills-heading">
        <h2>
          Your toolkit{" "}
          <span className="count-badge">{data.profile.skills.length}</span>
        </h2>
        <label className="search-field">
          <Icon name="search" size={17} />
          <input
            aria-label="Search skills"
            placeholder="Find a skill…"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>
      </div>
      <div className="skills-grid">
        {skills.map((skill, i) => {
          const evidence = skillEvidence(data.projects, skill);
          return (
            <details className="skill-card" key={skill}>
              <summary>
                <span className={`skill-symbol skill-symbol-${i % 3}`}>
                  <Icon
                    name={
                      i % 3 === 0 ? "sparkle" : i % 3 === 1 ? "code" : "grid"
                    }
                    size={22}
                  />
                </span>
                <span>
                  <b>{skill}</b>
                  <small>Self-reported</small>
                </span>
                <Icon name="down" size={17} />
              </summary>
              <p>
                {evidence.length
                  ? `${evidence.length} linked ${evidence.length === 1 ? "project" : "projects"} in your portfolio`
                  : "Add a project to show this skill in action."}
              </p>
              <div className="skill-evidence">
                {evidence.length ? (
                  evidence.map((project) => (
                    <button onClick={() => onProject(project)} key={project.id}>
                      <Icon name="folder" size={16} />
                      {project.name}
                      <Icon name="upRight" size={14} />
                    </button>
                  ))
                ) : (
                  <button onClick={() => onProject()}>
                    <Icon name="plus" size={16} />
                    Add a piece of proof
                  </button>
                )}
              </div>
            </details>
          );
        })}
      </div>
      {!skills.length && (
        <EmptyState
          icon="award"
          title={
            query
              ? "No skills match that search."
              : "You bring more than you think."
          }
          text="Start with the tools, methods, and abilities you use in your work."
        />
      )}
      <div className="inline-note">
        <Icon name="shield" />
        <p>
          {demo ? (
            "Example skills demonstrate how evidence connects to work. They are not verified credentials."
          ) : (
            "Skills are self-reported. Add project examples to show how you use them."
          )}
        </p>
      </div>
    </>
  );
}

export function ProfileView({
  data,
  demo,
  onProfile,
  onProject,
  notify,
}: ViewProps) {
  const [tab, setTab] = useState("projects");
  const visibleTab = tab;
  async function share() {
    if (!demo && (!data.public_handle || !data.published_at)) {
      notify(
        "Your profile is private. Open publishing controls to choose what to share.",
      );
      return;
    }
    try {
      await navigator.clipboard.writeText(
        `${window.location.origin}${demo ? "/explore/profile" : `/u/${encodeURIComponent(data.public_handle!)}`}`,
      );
      notify(
        demo
          ? "Example profile link copied. Your browser-only edits won’t be included for other visitors."
          : "Your public profile link is copied and ready to share.",
      );
    } catch {
      notify(
        "Your browser couldn’t copy the link. Use the address bar to copy this page’s URL.",
      );
    }
  }
  return (
    <>
      <PageHeading
        eyebrow="YOUR PROFESSIONAL IDENTITY"
        title="A profile that’s unmistakably you."
        text="Your work, your skills, your point of view. This is where they come together."
      >
        <button className="button secondary" onClick={share}>
          <Icon name="link" size={17} /> Share profile
        </button>
        <button className="button primary" onClick={onProfile}>
          <Icon name="edit" size={16} /> Edit profile
        </button>
      </PageHeading>
      <section className="profile-page-card">
        <div className="profile-cover">
          <span>
            BUILT ON ABILITY.
            <br />
            OPEN TO POSSIBILITY.
          </span>
        </div>
        <div className="profile-page-intro">
          <div className="avatar profile-avatar">
            {data.profile.full_name
              .split(" ")
              .map((part) => part[0])
              .slice(0, 2)
              .join("")
              .toLowerCase() || "you"}
          </div>
          <span className="profile-visibility">
            <Icon name={data.published_at ? "globe" : "lock"} size={14} />
            {demo
              ? "Example profile"
              : data.published_at
                ? "Public profile"
                : "Private profile"}
          </span>
          <h2>{data.profile.full_name || "Your name, your story."}</h2>
          <p className="profile-role">
            {data.profile.target_roles.join(" · ") ||
              "What do you want to be known for?"}
          </p>
          <p className="profile-tagline">
            {demo
              ? "Turning complex problems into simple, human experiences."
              : "Explore the work and skills behind my experience."}
          </p>
          <div className="profile-facts">
            <span>
              <Icon name="folder" size={16} />
              {
                data.projects.filter(
                  (project) => project.visibility === "public",
                ).length
              }{" "}
              public projects
            </span>
            <span>
              <Icon name="award" size={16} />
              {data.profile.skills.length} skills
            </span>
            <span>
              <Icon name="shield" size={16} />
              Shared on my terms
            </span>
          </div>
        </div>
        <div className="tabs profile-tabs">
          {[["projects", "Selected work"], ["skills", "Skills & experience"]].map(([key, item]) => (
            <button
              key={key}
              aria-pressed={visibleTab === key}
              onClick={() => setTab(key)}
            >
              {item}
            </button>
          ))}
        </div>
        <div className="profile-page-content">
          {visibleTab === "projects" ? (
            <>
              <div className="block-heading">
                <h3>The work behind the words.</h3>
                <span className="muted small">Public projects</span>
              </div>
              <div className="project-grid">
                {data.projects
                  .filter((project) => project.visibility === "public")
                  .map((project) => (
                    <ProjectCard
                      key={project.id}
                      project={project}
                      onClick={() => onProject(project)}
                    />
                  ))}
              </div>
              {!data.projects.some(
                (project) => project.visibility === "public",
              ) && (
                <EmptyState
                  icon="folder"
                  title="Your work deserves a place here."
                  text="When you choose to make a project public, it will appear in your profile."
                />
              )}
            </>
          ) : visibleTab === "skills" ? (
            <>
              <h3>What I bring to the table.</h3>
              <div className="profile-skill-list">
                {data.profile.skills.map((skill) => (
                  <div key={skill}>
                    <Icon name="award" size={20} />
                    <span>{skill}</span>
                    <small>
                      {skillEvidence(data.projects, skill).length} linked
                      projects · Self-reported
                    </small>
                  </div>
                ))}
              </div>
            </>
          ) : (
            <p className="muted">
              Choose projects or skills to show in your portfolio.
            </p>
          )}
        </div>
      </section>
      {!demo && (
        <div className="inline-note">
          <Icon name="shield" />
          <p>
            Ready to share?{" "}
            <Link href="/manage/projects">
              Manage your public address, project visibility, and sharing.
            </Link>
          </p>
        </div>
      )}
    </>
  );
}

export function SettingsView({
  data,
  demo,
  onProfile,
  onReset,
}: ViewProps & { onReset: () => void }) {
  return (
    <>
      <PageHeading
        eyebrow="MAKE YOURSELF AT HOME"
        title="Your workspace, your way."
        text="Manage your identity, privacy, and the details that matter."
      />
      <div className="settings-layout">
        <section className="settings-section">
          <div className="settings-section-title">
            <Icon name="user" />
            <div>
              <h2>Your identity</h2>
              <p>The essentials behind your professional profile.</p>
            </div>
          </div>
          <div className="settings-row">
            <div>
              <b>Profile details</b>
              <p>
                {data.profile.full_name || "Add your name"} ·{" "}
                {data.profile.target_roles[0] || "Choose your direction"}
              </p>
            </div>
            <button className="button secondary" onClick={onProfile}>
              Edit profile
            </button>
          </div>
        </section>
        <section className="settings-section">
          <div className="settings-section-title">
            <Icon name="shield" />
            <div>
              <h2>Privacy & control</h2>
              <p>Choose what you share. Keep the rest yours.</p>
            </div>
          </div>
          <div className="settings-row">
            <div>
              <b>Account data & privacy</b>
              <p>
                Export your information and choose what you share.
              </p>
            </div>
            <Link
              className="button secondary"
              href={demo ? "/signup" : "/account/privacy"}
            >
              {demo ? "Create an account" : "Manage privacy"}
              <Icon name="upRight" size={15} />
            </Link>
          </div>
          <div className="settings-row">
            <div>
              <b>Security & sessions</b>
              <p>Review signed-in devices and manage account access.</p>
            </div>
            <Link
              className="button secondary"
              href={demo ? "/login" : "/security/sessions"}
            >
              {demo ? "Sign in" : "Open security"}
              <Icon name="upRight" size={15} />
            </Link>
          </div>
        </section>
        {demo && (
          <section className="settings-section">
            <div className="settings-section-title">
              <Icon name="sparkle" />
              <div>
                <h2>Your example workspace</h2>
                <p>
                  Try things out. Make it yours. Start fresh whenever you like.
                </p>
              </div>
            </div>
            <div className="settings-row">
              <div>
                <b>Reset example activity</b>
                <p>
                  Restore the original example profile and projects.
                </p>
              </div>
              <button className="button secondary" onClick={onReset}>
                Reset example
              </button>
            </div>
          </section>
        )}
        <div className="settings-about">
          <BrandMarkSmall />
          <div>
            <b>Built around what you can do.</b>
            <p>
              Proofpath brings your professional identity, work, and skills
              together.
            </p>
          </div>
          <Link href="/#faq" className="text-link">
            Help & resources <Icon name="arrow" size={16} />
          </Link>
        </div>
      </div>
    </>
  );
}

function BrandMarkSmall() {
  return (
    <span className="soft-icon">
      <Icon name="sparkle" size={25} />
    </span>
  );
}
