"use client";

import Link from "next/link";
import { useEffect, useState, type FormEvent } from "react";
import { api, messageOf } from "../lib/api";
import { BrandMark, Icon } from "./icons";
import { ProjectArt } from "./project-art";

type Metadata = {
  registration_enabled: boolean;
  terms_url: string | null;
  terms_version: string | null;
};
type Session = { user: { initial_purpose: string }; next_action: string };

export function AuthPage({ mode }: { mode: "login" | "signup" }) {
  const signup = mode === "signup";
  const [meta, setMeta] = useState<Metadata | null>(null);
  const [showPassword, setShowPassword] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState(false);
  useEffect(() => {
    if (!signup) return;
    const controller = new AbortController();
    api<Metadata>("/meta/public", "GET", undefined, controller.signal)
      .then((result) => {
        setMeta(result);
        if (!result.registration_enabled)
          setError(
            "Account creation is currently unavailable. You can explore the example workspace in the meantime.",
          );
      })
      .catch((reason) => {
        if (!controller.signal.aborted) setError(messageOf(reason));
      });
    return () => controller.abort();
  }, [signup]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    if (signup && (!meta?.registration_enabled || !meta.terms_version)) return;
    setBusy(true);
    setError("");
    const values = new FormData(event.currentTarget);
    try {
      if (signup) {
        await api("/auth/register", "POST", {
          full_name: String(values.get("full_name")).trim(),
          email: values.get("email"),
          password: values.get("password"),
          initial_purpose: "candidate",
          terms_version: meta!.terms_version,
        });
        setSuccess(true);
      } else {
        const result = await api<Session>("/auth/login", "POST", {
          email: values.get("email"),
          password: values.get("password"),
        });
        window.location.assign(
          result.next_action === "candidate_onboarding"
            ? "/candidate/onboarding"
            : "/profile",
        );
      }
    } catch (reason) {
      setError(messageOf(reason));
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="auth-page" id="main-content">
      <section className="auth-form-side">
        <Link className="app-brand" href="/">
          <BrandMark />
          <span>
            proofpath<span className="brand-period">.</span>
          </span>
        </Link>
        <div className="auth-form-content">
          {success ? (
            <div className="auth-success">
              <span className="empty-icon">
                <Icon name="mail" size={30} />
              </span>
              <div className="eyebrow">ONE SMALL STEP LEFT</div>
              <h1>Check your inbox.</h1>
              <p>
                If your email address is eligible, you’ll receive a verification
                link. Open it to finish setting up your account.
              </p>
              <Link className="button primary" href="/login">
                Back to sign in <Icon name="arrow" size={16} />
              </Link>
            </div>
          ) : (
            <>
              <Link href="/" className="auth-back">
                <Icon name="arrow" size={14} /> Back to Proofpath
              </Link>
              <div className="eyebrow">
                {signup
                  ? "YOUR NEXT CHAPTER STARTS HERE"
                  : "GOOD TO SEE YOU AGAIN"}
              </div>
              <h1>
                {signup ? "There’s more to your story." : "Welcome back."}
              </h1>
              <p className="auth-intro">
                {signup
                  ? "Give your work, your skills, and your potential a place to grow."
                  : "Pick up where you left off. Your next possibility is waiting."}
              </p>
              <form className="editor-form auth-form" onSubmit={submit}>
                {signup && (
                  <label>
                    Full name
                    <input
                      name="full_name"
                      required
                      maxLength={120}
                      autoComplete="name"
                      placeholder="Your name"
                    />
                  </label>
                )}
                <label>
                  Email address
                  <input
                    type="email"
                    name="email"
                    required
                    maxLength={254}
                    autoComplete="email"
                    placeholder="you@example.com"
                  />
                </label>
                <label>
                  <span className="password-label">
                    Password
                    {!signup && (
                      <Link href="/reset-password">Forgot password?</Link>
                    )}
                  </span>
                  <span className="password-input">
                    <input
                      type={showPassword ? "text" : "password"}
                      name="password"
                      required
                      minLength={signup ? 12 : undefined}
                      maxLength={128}
                      autoComplete={
                        signup ? "new-password" : "current-password"
                      }
                      placeholder={
                        signup ? "Create a strong password" : "Your password"
                      }
                    />
                    <button
                      type="button"
                      className="icon-button"
                      onClick={() => setShowPassword(!showPassword)}
                      aria-label={
                        showPassword ? "Hide password" : "Show password"
                      }
                      aria-pressed={showPassword}
                    >
                      <Icon name={showPassword ? "lock" : "eye"} size={18} />
                    </button>
                  </span>
                  {signup && <small>Use at least 12 characters.</small>}
                </label>
                {signup && (
                  <label className="checkbox-label">
                    <input name="terms" type="checkbox" required />
                    <span>
                      I agree to the{" "}
                      {meta?.terms_url &&
                      /^https?:\/\//.test(meta.terms_url) ? (
                        <a
                          href={meta.terms_url}
                          target="_blank"
                          rel="noopener noreferrer"
                        >
                          Terms of Service
                        </a>
                      ) : (
                        "Terms of Service"
                      )}
                      .
                    </span>
                  </label>
                )}
                {error && (
                  <div role="alert" className="form-error">
                    {error}
                  </div>
                )}
                <button
                  className="button primary auth-submit"
                  disabled={busy || (signup && !meta?.registration_enabled)}
                >
                  {busy
                    ? signup
                      ? "Creating your account…"
                      : "Signing in…"
                    : signup
                      ? "Create your account"
                      : "Sign in"}
                  <Icon name="arrow" size={17} />
                </button>
              </form>
              <p className="auth-switch">
                {signup ? "Already part of Proofpath?" : "New here?"}{" "}
                <Link href={signup ? "/login" : "/signup"}>
                  {signup ? "Sign in" : "Create an account"}
                </Link>
              </p>
              <div className="auth-explore">
                <span>Just looking around?</span>
                <Link href="/explore">
                  Explore an example workspace <Icon name="upRight" size={14} />
                </Link>
              </div>
            </>
          )}
        </div>
        <p className="auth-footnote">
          <Icon name="shield" size={15} /> Your work. Your data. Your choice.
        </p>
      </section>
      <aside className="auth-story">
        <div className="eyebrow">BUILT AROUND WHAT YOU CAN DO</div>
        <h2>
          Your work
          <br />
          has a story.
          <br />
          <em>Let it speak.</em>
        </h2>
        <div className="auth-art">
          <ProjectArt kind="folio" />
        </div>
        <div className="auth-story-bottom">
          <span className="note-star">✳</span>
          <p>
            Not just where you’ve been.
            <br />
            <strong>What you’re capable of next.</strong>
          </p>
          <span>01 — ∞</span>
        </div>
      </aside>
    </main>
  );
}
