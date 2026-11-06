"use client";

import Link from "next/link";
import { useEffect, useState, type FormEvent } from "react";
import { api, messageOf } from "../lib/api";
import type { Profile } from "../lib/workspace-data";
import { BrandMark, Icon } from "./icons";

export function OnboardingPage() {
  const [profile, setProfile] = useState<Profile | null>(null);
  const [step, setStep] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [name, setName] = useState("");
  const [roles, setRoles] = useState("");
  const [skills, setSkills] = useState("");
  const split = (value: string) => [
    ...new Set(
      value
        .split(",")
        .map((item) => item.trim())
        .filter(Boolean),
    ),
  ];
  useEffect(() => {
    const controller = new AbortController();
    api<Profile>(
      "/me/candidate/onboarding",
      "GET",
      undefined,
      controller.signal,
    )
      .then((result) => {
        setProfile(result);
        setName(result.full_name);
        setRoles(result.target_roles.join(", "));
        setSkills(result.skills.join(", "));
      })
      .catch((reason) => {
        if (!controller.signal.aborted) setError(messageOf(reason));
      });
    return () => controller.abort();
  }, []);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!profile || busy) return;
    setError("");
    if (!name.trim()) {
      setError("Add your name to get started.");
      return;
    }
    if (split(roles).length > 5 || split(skills).length > 20) {
      setError("Choose up to 5 target roles and 20 skills.");
      return;
    }
    if (step < 2) {
      setStep(step + 1);
      return;
    }
    setBusy(true);
    try {
      const draft = await api<Profile>("/me/candidate/onboarding", "PATCH", {
        full_name: name.trim(),
        target_roles: split(roles),
        skills: split(skills),
        expected_version: profile.version,
      });
      setProfile(draft);
      const complete = await api<Profile>(
        "/me/candidate/onboarding/complete",
        "POST",
        { expected_version: draft.version },
      );
      setProfile(complete);
    } catch (reason) {
      setError(messageOf(reason));
    } finally {
      setBusy(false);
    }
  }

  return (
    <main id="main-content" className="onboarding-page">
      <header>
        <Link className="app-brand" href="/">
          <BrandMark />
          <span>
            proofpath<span className="brand-period">.</span>
          </span>
        </Link>
        <Link href="/#faq" className="text-link">
          A little help? <Icon name="help" size={16} />
        </Link>
      </header>
      <section className="onboarding-card">
        {profile?.status === "complete" ? (
          <div className="onboarding-complete">
            <span className="empty-icon">
              <Icon name="check" size={30} />
            </span>
            <div className="eyebrow">A GOOD PLACE TO START</div>
            <h1>Your next chapter is open.</h1>
            <p>
              You’ve made your introduction. Now add a project, explore your
              skills, and see where your work can take you.
            </p>
            <Link className="button primary" href="/profile">
              Open your workspace <Icon name="arrow" size={17} />
            </Link>
          </div>
        ) : (
          <>
            <div
              className="onboarding-steps"
              aria-label={`Step ${step + 1} of 3`}
            >
              {["Your introduction", "Your toolkit", "Your next chapter"].map(
                (label, i) => (
                  <div key={label} className={i <= step ? "active" : ""}>
                    <span>
                      {i < step ? <Icon name="check" size={13} /> : `0${i + 1}`}
                    </span>
                    <b>{label}</b>
                  </div>
                ),
              )}
            </div>
            <div className="eyebrow">LET’S START WITH YOU</div>
            <h1>
              {
                [
                  "Tell us a little about yourself.",
                  "What do you bring to the table?",
                  "Looking like you already.",
                ][step]
              }
            </h1>
            <p className="muted">
              {
                [
                  "You don’t need to have it all figured out. A name and a direction is a good start.",
                  "Add the skills and tools you use. You can connect them to your work next.",
                  "Here’s your introduction. Your profile stays private until you decide to share it.",
                ][step]
              }
            </p>
            {profile ? (
              <form className="editor-form" onSubmit={submit}>
                {step === 0 && (
                  <>
                    <label>
                      What should we call you?
                      <input
                        autoFocus
                        required
                        maxLength={120}
                        autoComplete="name"
                        value={name}
                        onChange={(event) => setName(event.target.value)}
                        placeholder="Your full name"
                      />
                    </label>
                    <label>
                      What kind of work interests you?
                      <input
                        maxLength={500}
                        value={roles}
                        onChange={(event) => setRoles(event.target.value)}
                        placeholder="e.g. Product Designer, Frontend Engineer"
                      />
                      <small>
                        Up to five roles, separated by commas. It’s okay to
                        leave this open.
                      </small>
                    </label>
                  </>
                )}
                {step === 1 && (
                  <>
                    <label>
                      Your skills & tools
                      <textarea
                        autoFocus
                        rows={4}
                        maxLength={2000}
                        value={skills}
                        onChange={(event) => setSkills(event.target.value)}
                        placeholder="e.g. Python, User research, React, Communication"
                      />
                      <small>
                        Add up to 20 skills, separated by commas. These are
                        self-reported.
                      </small>
                    </label>
                    <div className="skill-tags">
                      {split(skills).map((skill) => (
                        <span key={skill}>{skill}</span>
                      ))}
                    </div>
                  </>
                )}
                {step === 2 && (
                  <div className="onboarding-review">
                    <span className="avatar avatar-large">
                      {name
                        .split(" ")
                        .map((part) => part[0])
                        .slice(0, 2)
                        .join("")
                        .toLowerCase()}
                    </span>
                    <h2>{name}</h2>
                    <p>{split(roles).join(" · ") || "Open to possibilities"}</p>
                    <div className="skill-tags">
                      {split(skills).map((skill) => (
                        <span key={skill}>{skill}</span>
                      ))}
                    </div>
                  </div>
                )}
                {error && (
                  <p className="form-error" role="alert">
                    {error}
                  </p>
                )}
                <div className="dialog-footer">
                  {step > 0 && (
                    <button
                      type="button"
                      className="button secondary"
                      disabled={busy}
                      onClick={() => setStep(step - 1)}
                    >
                      Back
                    </button>
                  )}
                  <button className="button primary" disabled={busy}>
                    {busy
                      ? "Building your workspace…"
                      : step === 2
                        ? "Make it official"
                        : "Continue"}
                    <Icon name="arrow" size={16} />
                  </button>
                </div>
              </form>
            ) : error ? (
              <div className="onboarding-recovery">
                <p className="form-error" role="alert">
                  {error}
                </p>
                <Link className="button primary" href="/login">
                  Sign in to continue <Icon name="arrow" size={16} />
                </Link>
                <Link className="text-link" href="/explore">
                  Explore an example first
                </Link>
              </div>
            ) : (
              <p role="status" className="muted">
                Getting your introduction ready…
              </p>
            )}
          </>
        )}
        <div className="onboarding-privacy">
          <Icon name="shield" size={17} />
          <span>Your story belongs to you. Nothing is public by default.</span>
        </div>
      </section>
    </main>
  );
}
