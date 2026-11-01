"use client";

import Link from "next/link";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { api, ApiError, messageOf } from "../lib/api";
import type { Job } from "../lib/workspace-data";
import { AppHeader } from "./app-header";
import { Icon } from "./icons";

export function JobPage({ jobId }: { jobId: string }) {
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [saving, setSaving] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [access, setAccess] = useState<
    "loading" | "ready" | "signin" | "profile"
  >("loading");
  const [notice, setNotice] = useState("");
  const requestKey = useRef("");
  useEffect(() => {
    const controller = new AbortController();
    api<Job>(
      `/jobs/${encodeURIComponent(jobId)}`,
      "GET",
      undefined,
      controller.signal,
    )
      .then(setJob)
      .catch((reason) => {
        if (!controller.signal.aborted) setError(messageOf(reason));
      });
    async function loadAccount() {
      try {
        const portfolio = await api<{
          published_at: string | null;
          public_handle: string | null;
        }>("/me/portfolio", "GET", undefined, controller.signal);
        setAccess(
          portfolio.published_at && portfolio.public_handle
            ? "ready"
            : "profile",
        );
        const savedJobs = await api<{ jobs: { id: string }[] }>(
          "/me/saved-jobs",
          "GET",
          undefined,
          controller.signal,
        );
        setSaved(savedJobs.jobs.some((item) => item.id === jobId));
      } catch (reason) {
        if (!controller.signal.aborted)
          setAccess(
            reason instanceof ApiError && reason.status === 403
              ? "profile"
              : "signin",
          );
      }
    }
    void loadAccount();
    return () => controller.abort();
  }, [jobId]);
  async function save() {
    if (saving) return;
    setSaving(true);
    setNotice("");
    try {
      await api(
        `/me/saved-jobs/${encodeURIComponent(jobId)}`,
        saved ? "DELETE" : "PUT",
      );
      setSaved(!saved);
      setNotice(
        saved
          ? "Role removed from your saved opportunities."
          : "Saved for when you’re ready to take the next step.",
      );
    } catch (reason) {
      setNotice(messageOf(reason));
    } finally {
      setSaving(false);
    }
  }
  async function apply(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy || submitted || access !== "ready") return;
    setBusy(true);
    setNotice("");
    if (!requestKey.current) requestKey.current = crypto.randomUUID();
    const values = new FormData(event.currentTarget);
    try {
      await api(`/jobs/${encodeURIComponent(jobId)}/applications`, "POST", {
        request_key: requestKey.current,
        portfolio_consent_version: "job-application-public-portfolio-v1",
        cover_note: String(values.get("cover_note") || "").trim(),
      });
      setSubmitted(true);
    } catch (reason) {
      setNotice(messageOf(reason));
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <AppHeader section="public" activePath="/jobs" />
      <main id="main-content" className="public-job-page page-width">
        <Link href="/jobs" className="text-link">
          <Icon name="arrow" className="back-arrow" size={16} /> All
          opportunities
        </Link>
        {error ? (
          <div className="workspace-error">
            <span className="empty-icon">
              <Icon name="briefcase" size={26} />
            </span>
            <h1>This opportunity couldn’t load.</h1>
            <p>{error}</p>
            <button
              className="button primary"
              onClick={() => window.location.reload()}
            >
              Try again
            </button>
          </div>
        ) : !job ? (
          <div className="loading-workspace" role="status">
            <h1>Finding your next possibility…</h1>
            <div className="skeleton skeleton-wide" />
          </div>
        ) : (
          <div className="public-job-layout">
            <article className="public-job-content">
              <span className="company-logo logo-peach">
                {job.organization_name[0]}
              </span>
              <div className="eyebrow">{job.organization_name}</div>
              <h1>{job.title}</h1>
              <p className="public-job-intro">
                Bring your perspective. Show what you can do.
              </p>
              <div className="detail-divider" />
              <h2>About the opportunity</h2>
              <p className="job-full-description">{job.description}</p>
              <h2>Skills for this role</h2>
              {job.competency_requirements.length ? (
                <div className="job-requirements">
                  {job.competency_requirements.map((skill) => (
                    <div key={skill.competency_name}>
                      <Icon name="award" />
                      <span>{skill.competency_name}</span>
                      <small>{skill.target_level}</small>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="muted">
                  The team hasn’t listed structured skill requirements for this
                  role.
                </p>
              )}
              <div className="application-note">
                <Icon name="shield" />
                <p>
                  Your application shares a private copy of your published
                  portfolio. Assessment evidence has its own sharing controls.
                  People make the hiring decisions.
                </p>
              </div>
            </article>
            <aside className="job-application-card">
              <h2>
                {submitted
                  ? "Your next chapter is in motion."
                  : "Make your introduction."}
              </h2>
              {submitted ? (
                <div className="success-panel">
                  <Icon name="check" />
                  <div>
                    <p>
                      Your application has been submitted to{" "}
                      {job.organization_name}.
                    </p>
                    <Link href="/applications" className="text-link">
                      Track your application <Icon name="arrow" size={16} />
                    </Link>
                  </div>
                </div>
              ) : access === "loading" ? (
                <p role="status" className="muted">
                  Checking your profile…
                </p>
              ) : access === "signin" ? (
                <>
                  <p>
                    Sign in to share your work and apply for this opportunity.
                  </p>
                  <Link href="/login" className="button primary">
                    Sign in to apply <Icon name="arrow" size={16} />
                  </Link>
                  <Link href="/signup" className="text-link">
                    New here? Create your profile
                  </Link>
                </>
              ) : access === "profile" ? (
                <>
                  <p>
                    Complete your profile and choose the work you want to share
                    before applying.
                  </p>
                  <Link href="/manage/projects" className="button primary">
                    Prepare your portfolio <Icon name="arrow" size={16} />
                  </Link>
                </>
              ) : (
                <form className="editor-form" onSubmit={apply}>
                  <p>
                    Your work is the starting point. Add a little context if
                    you’d like.
                  </p>
                  <label>
                    A note to the team <small>Optional</small>
                    <textarea
                      name="cover_note"
                      rows={5}
                      maxLength={2000}
                      placeholder="What draws you to this role?"
                    />
                  </label>
                  <label className="checkbox-label">
                    <input name="consent" type="checkbox" required />
                    <span>
                      I agree to share a private copy of my published portfolio
                      with {job.organization_name} for this application.
                    </span>
                  </label>
                  <button className="button primary" disabled={busy}>
                    {busy ? "Submitting…" : "Submit application"}
                    <Icon name="arrow" size={16} />
                  </button>
                </form>
              )}
              {!submitted && (
                <button
                  className="button secondary"
                  disabled={saving}
                  onClick={save}
                >
                  <Icon name="bookmark" size={16} />
                  {saving
                    ? "Saving…"
                    : saved
                      ? "Remove saved role"
                      : "Save for later"}
                </button>
              )}
              {notice && (
                <p className="form-error" role="status">
                  {notice}
                </p>
              )}
              <div className="privacy-note">
                <Icon name="shield" size={19} />
                <p>
                  Your work, shared with care.
                  <br />
                  <span>You control your evidence.</span>
                </p>
              </div>
            </aside>
          </div>
        )}
      </main>
    </>
  );
}
