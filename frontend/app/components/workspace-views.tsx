"use client";

import Link from "next/link";
import { useEffect, useState, type ReactNode } from "react";
import {
  matchingJobs,
  skillEvidence,
  type Job,
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

function JobCard({
  job,
  saved,
  onSave,
  onOpen,
  compact = false,
}: {
  job: Job;
  saved: boolean;
  onSave: () => void;
  onOpen: () => void;
  compact?: boolean;
}) {
  return (
    <article className={`job-card ${compact ? "job-card-compact" : ""}`}>
      <div className="job-card-top">
        <div className={`company-logo logo-${job.color || "peach"}`}>
          {job.organization_name === "Layers" ? "≋" : job.organization_name[0]}
        </div>
        <div>
          <span className="company-name">{job.organization_name}</span>
          {compact && (
            <button onClick={onOpen} className="job-title">
              {job.title}
            </button>
          )}
        </div>
        <button
          className={`icon-button save-button ${saved ? "is-saved" : ""}`}
          aria-label={`${saved ? "Unsave" : "Save"} ${job.title} at ${job.organization_name}`}
          aria-pressed={saved}
          onClick={onSave}
        >
          <Icon name="bookmark" size={19} />
        </button>
      </div>
      {!compact && (
        <>
          <button onClick={onOpen} className="job-title">
            {job.title}
          </button>
          <p className="job-description">{job.description}</p>
          <div className="skill-tags">
            {job.competency_requirements.slice(0, 3).map((skill) => (
              <span key={skill.competency_name}>{skill.competency_name}</span>
            ))}
          </div>
        </>
      )}
      <div className="job-card-footer">
        <span>
          <Icon name="location" size={14} />
          {job.location || "Location not listed"}
        </span>
        {job.salary && <strong>{job.salary}</strong>}
        <button
          onClick={onOpen}
          className="icon-button"
          aria-label={`View ${job.title}`}
        >
          <Icon name="arrow" size={16} />
        </button>
      </div>
    </article>
  );
}

export function DashboardView(props: ViewProps) {
  const { data, demo, href, onProject, onJob, onSave } = props;
  const firstName = data.profile.full_name.split(" ")[0] || "there";
  const completion = [
    !!data.profile.full_name,
    !!data.profile.target_roles.length,
    !!data.profile.skills.length,
    !!data.projects.length,
  ];
  const progress = completion.filter(Boolean).length * 25;
  return (
    <>
      <PageHeading
        eyebrow="YOUR POTENTIAL, IN PROGRESS"
        title={`A little further, ${firstName}.`}
        text="Good work adds up. Here’s where your next chapter begins."
      >
        <Link className="button secondary" href={href("profile")}>
          <Icon name="eye" size={17} /> View my profile
        </Link>
      </PageHeading>
      <section className="dashboard-hero">
        <div>
          <h2>Make room for your next project.</h2>
          <p>Add your latest work. Show the thinking behind it.</p>
        </div>
        <button className="button primary" onClick={() => onProject()}>
          <Icon name="plus" size={16} /> Add a project
        </button>
      </section>
      <section
        className="workspace-stats"
        aria-label="Your workspace at a glance"
      >
        {[
          {
            icon: "folder" as const,
            label: "Projects in your portfolio",
            value: data.projects.length,
            key: "projects" as const,
            detail: `${data.projects.filter((project) => project.visibility === "public").length} shared publicly`,
          },
          {
            icon: "award" as const,
            label: "Skills in your toolkit",
            value: data.profile.skills.length,
            key: "skills" as const,
            detail: "Grounded in your experience",
          },
          {
            icon: "briefcase" as const,
            label: "Opportunities saved",
            value: data.saved.length,
            key: "opportunities" as const,
            detail: "Your next move, on your terms",
          },
        ].map((stat) => (
          <Link href={href(stat.key)} className="stat-item" key={stat.label}>
            <span className="stat-icon">
              <Icon name={stat.icon} />
            </span>
            <div>
              <span>{stat.label}</span>
              <strong>{String(stat.value).padStart(2, "0")}</strong>
              <small>{stat.detail}</small>
            </div>
            <Icon name="upRight" size={17} />
          </Link>
        ))}
      </section>
      <div className="dashboard-columns">
        <div className="dashboard-primary">
          <section>
            <div className="block-heading">
              <div>
                <h2>
                  Your work, in the spotlight
                  <span className="heading-dot">.</span>
                </h2>
                <p>More than projects. Proof of what you bring.</p>
              </div>
              <Link className="text-link" href={href("projects")}>
                View all <Icon name="arrow" size={15} />
              </Link>
            </div>
            {data.projects.length ? (
              <div className="project-grid">
                {data.projects.slice(0, 2).map((project) => (
                  <ProjectCard
                    key={project.id}
                    project={project}
                    onClick={() => onProject(project)}
                  />
                ))}
              </div>
            ) : (
              <EmptyState
                icon="folder"
                title="Your first piece of proof belongs here."
                text="Start with a project you’re proud of. It doesn’t have to be perfect."
              >
                <button
                  className="button secondary"
                  onClick={() => onProject()}
                >
                  Add your first project
                </button>
              </EmptyState>
            )}
          </section>
          <section className="dashboard-jobs">
            <div className="block-heading">
              <div>
                <h2>
                  A few new possibilities<span className="heading-dot">.</span>
                </h2>
                <p>
                  {demo
                    ? "Example opportunities for your next chapter."
                    : "Explore teams and the work they’re building."}
                </p>
              </div>
              <Link className="text-link" href={href("opportunities")}>
                Explore <Icon name="arrow" size={15} />
              </Link>
            </div>
            {data.jobs.length ? (
              data.jobs
                .slice(0, 2)
                .map((job) => (
                  <JobCard
                    key={job.id}
                    job={job}
                    compact
                    saved={data.saved.includes(job.id)}
                    onSave={() => onSave(job)}
                    onOpen={() => onJob(job)}
                  />
                ))
            ) : (
              <EmptyState
                icon="briefcase"
                title="New possibilities are on the way."
                text="Published opportunities will appear here."
              />
            )}
          </section>
        </div>
        <aside className="dashboard-secondary">
          <section className="profile-progress-card">
            <div className="block-heading">
              <h3>A little more you.</h3>
              <Icon name="sparkle" size={19} />
            </div>
            <p>A complete profile helps people see the whole picture.</p>
            <div
              className="progress-circle"
              style={{
                background: `conic-gradient(var(--brand) ${progress}%, #f1e9e3 0)`,
              }}
            >
              <div>
                <strong>{progress}%</strong>
                <span>profile essentials</span>
              </div>
            </div>
            <ul className="completion-list">
              {[
                "Make your introduction",
                "Set your direction",
                "Add your skills",
                "Show your work",
              ].map((label, i) => (
                <li key={label}>
                  <span className={completion[i] ? "complete" : ""}>
                    {completion[i] ? <Icon name="check" size={12} /> : i + 1}
                  </span>
                  {label}
                </li>
              ))}
            </ul>
            <Link href={href("profile")} className="button secondary">
              {progress === 100 ? "See your profile" : "Complete your profile"}
              <Icon name="arrow" size={16} />
            </Link>
          </section>
          <div className="privacy-note">
            <Icon name="shield" size={20} />
            <p>
              Your work. Your choice.
              <br />
              <span>You decide what the world sees.</span>
            </p>
          </div>
        </aside>
      </div>
    </>
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

export function JobsView({ data, demo, onJob, onSave }: ViewProps) {
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState("All roles");
  const [remote, setRemote] = useState(false);
  const [savedOnly, setSavedOnly] = useState(false);
  useEffect(() => {
    setQuery(new URLSearchParams(window.location.search).get("q") || "");
  }, []);
  const jobs = matchingJobs(
    data.jobs,
    query,
    category,
    remote,
    savedOnly,
    data.saved,
  );
  return (
    <>
      <PageHeading
        eyebrow="YOUR NEXT CHAPTER"
        title="Find work that feels like you."
        text="Explore possibilities. Find your people. Bring what makes you different."
      />
      <section className="opportunity-search">
        <label>
          <Icon name="search" size={21} />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search by role, company, or skill"
            aria-label="Search by role, company, or skill"
          />
          {query && (
            <button
              className="icon-button"
              onClick={() => setQuery("")}
              aria-label="Clear search"
            >
              <Icon name="close" size={17} />
            </button>
          )}
        </label>
        {demo && (
          <label className="location-select">
            <Icon name="location" size={19} />
            <select
              aria-label="Work location"
              value={remote ? "remote" : "all"}
              onChange={(event) => setRemote(event.target.value === "remote")}
            >
              <option value="all">Any location</option>
              <option value="remote">Remote only</option>
            </select>
          </label>
        )}
      </section>
      <div className="filter-bar opportunity-filters">
        <div className="tabs">
          <button aria-pressed={!savedOnly} onClick={() => setSavedOnly(false)}>
            Explore opportunities
          </button>
          <button aria-pressed={savedOnly} onClick={() => setSavedOnly(true)}>
            <Icon name="bookmark" size={15} /> Saved{" "}
            <span>{data.saved.length}</span>
          </button>
        </div>
        {demo && (
          <label className="category-select">
            <Icon name="filter" size={16} />
            <select
              aria-label="Role category"
              value={category}
              onChange={(event) => setCategory(event.target.value)}
            >
              {["All roles", "Design", "Engineering", "Research"].map(
                (item) => (
                  <option key={item}>{item}</option>
                ),
              )}
            </select>
          </label>
        )}
      </div>
      <div className="results-count" role="status">
        <strong>
          {jobs.length} {savedOnly ? "saved " : ""}
          {jobs.length === 1 ? "opportunity" : "opportunities"}
        </strong>
        <span>
          {demo
            ? "Example roles · For exploring the experience"
            : "Published by hiring teams"}
        </span>
      </div>
      {jobs.length ? (
        <div className="jobs-grid">
          {jobs.map((job) => (
            <JobCard
              key={job.id}
              job={job}
              saved={data.saved.includes(job.id)}
              onSave={() => onSave(job)}
              onOpen={() => onJob(job)}
            />
          ))}
        </div>
      ) : (
        <EmptyState
          icon="briefcase"
          title={
            savedOnly
              ? "Your shortlist starts here."
              : "No opportunities match just yet."
          }
          text={
            savedOnly
              ? "Save roles that catch your eye. They’ll be here when you’re ready."
              : "Try a different skill or clear your filters to see more possibilities."
          }
        >
          <button
            className="button secondary"
            onClick={() => {
              setQuery("");
              setCategory("All roles");
              setRemote(false);
              setSavedOnly(false);
            }}
          >
            Explore all roles <Icon name="arrow" size={16} />
          </button>
        </EmptyState>
      )}
      <div className="opportunity-bottom">
        <Icon name="sparkle" size={22} />
        <div>
          <b>Your next opportunity might start with your profile.</b>
          <p>Let your projects show what a job title can’t.</p>
        </div>
        <Link
          className="text-link"
          href={demo ? "/explore/profile" : "/profile"}
        >
          Make it yours <Icon name="arrow" size={16} />
        </Link>
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
            Verified assessment results are a separate kind of evidence.
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
            <>
              Assessment invitations and verified evidence are available in{" "}
              <Link href="/manage/applications">
                your application workspace
              </Link>
              .
            </>
          )}
        </p>
      </div>
    </>
  );
}

export function ApplicationsView({ data, demo, href, onJob }: ViewProps) {
  const [tab, setTab] = useState("All applications");
  const archived = ["rejected", "withdrawn", "hired"];
  const applications = data.applications.filter(
    (item) =>
      tab === "All applications" ||
      (tab === "Active"
        ? !archived.includes(item.status)
        : archived.includes(item.status)),
  );
  return (
    <>
      <PageHeading
        eyebrow="GOOD THINGS TAKE THEIR OWN PATH"
        title="Your next move, in motion."
        text="Keep track of the opportunities you’re exploring and what comes next."
      >
        <Link className="button secondary" href={href("opportunities")}>
          Find opportunities <Icon name="arrow" size={17} />
        </Link>
      </PageHeading>
      <div className="application-summary">
        {[
          {
            label: "In the pipeline",
            number: data.applications.filter(
              (item) => !archived.includes(item.status),
            ).length,
            icon: "briefcase" as const,
          },
          {
            label: "In conversation",
            number: data.applications.filter((item) =>
              ["in_review", "assessment", "interview"].includes(item.status),
            ).length,
            icon: "users" as const,
          },
          {
            label: "Offers received",
            number: data.applications.filter((item) => item.status === "offer")
              .length,
            icon: "sparkle" as const,
          },
        ].map((item) => (
          <div key={item.label}>
            <Icon name={item.icon} />
            <strong>{String(item.number).padStart(2, "0")}</strong>
            <span>{item.label}</span>
          </div>
        ))}
      </div>
      <div className="filter-bar">
        <div className="tabs">
          {["All applications", "Active", "Archived"].map((item) => (
            <button
              aria-pressed={tab === item}
              key={item}
              onClick={() => setTab(item)}
            >
              {item}
            </button>
          ))}
        </div>
        <span className="muted small">
          {demo ? "Example application activity" : "Your applications"}
        </span>
      </div>
      {applications.length ? (
        <div className="application-list">
          <div className="application-table-header">
            <span>OPPORTUNITY</span>
            <span>APPLIED</span>
            <span>STATUS</span>
            <span />
          </div>
          {applications.map((application) => (
            <article className="application-row" key={application.id}>
              <div>
                <span className="company-logo logo-peach">
                  {application.organization_name[0]}
                </span>
                <div>
                  <h3>{application.job_title}</h3>
                  <p>{application.organization_name}</p>
                </div>
              </div>
              <time dateTime={application.submitted_at}>
                {new Date(application.submitted_at).toLocaleDateString(
                  "en-US",
                  { month: "short", day: "numeric", year: "numeric" },
                )}
              </time>
              <span className={`status-pill status-${application.status}`}>
                <span />
                {application.status.replaceAll("_", " ")}
              </span>
              {demo ? (
                <button
                  className="icon-button"
                  aria-label={`View application for ${application.job_title}`}
                  onClick={() => {
                    const job = data.jobs.find(
                      (item) => item.id === application.job_id,
                    );
                    if (job) onJob(job);
                  }}
                >
                  <Icon name="arrow" size={18} />
                </button>
              ) : (
                <Link
                  href="/manage/applications"
                  className="icon-button"
                  aria-label={`Manage application for ${application.job_title}`}
                >
                  <Icon name="arrow" size={18} />
                </Link>
              )}
            </article>
          ))}
        </div>
      ) : (
        <EmptyState
          icon="mail"
          title="A new chapter is waiting."
          text="When you apply for an opportunity, you can follow its progress here."
        >
          <Link className="button primary" href={href("opportunities")}>
            Explore opportunities <Icon name="arrow" size={17} />
          </Link>
        </EmptyState>
      )}
      <div className="inline-note">
        <Icon name="shield" />
        <p>
          {demo ? (
            "This is a practice space. Example applications are saved in this browser and are never sent to employers."
          ) : (
            <>
              You’re in control.{" "}
              <Link href="/manage/applications">
                Manage evidence sharing, assessment invitations, and withdrawal.
              </Link>
            </>
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
              Manage your public address, project visibility, and employer
              discovery.
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
                Manage discovery, export your information, and control your
                data.
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
                  Restore the original projects, profile, and sample
                  applications.
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
              Proofpath brings your work, skills, and next opportunities
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
