"use client";

import { useEffect, useState, type FormEvent } from "react";
import { api, messageOf } from "../lib/api";
import type { ResumeFields, ResumeSnapshot } from "../lib/workspace-data";
import { Icon } from "./icons";
import type { ViewProps } from "./workspace";

const fieldLabels: [keyof Pick<ResumeFields, "education" | "experience" | "projects" | "certifications">, string][] = [
  ["education", "Education"], ["experience", "Experience"], ["projects", "Projects"], ["certifications", "Certifications"],
];
const demoSkills = ["Python", "FastAPI", "React", "TypeScript", "SQL", "PostgreSQL", "AWS", "Docker", "Machine Learning", "Communication", "Leadership"];

function lines(value: string) {
  return value.split("\n").map((item) => item.trim()).filter(Boolean);
}

function suggest(text: string): ResumeFields {
  const textLines = text.split("\n").map((line) => line.trim()).filter(Boolean);
  const email = text.match(/[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}/)?.[0] || "";
  const phone = text.match(/(?:\+?\d[\d ()-]{7,}\d)/)?.[0] || "";
  const skills = demoSkills.filter((skill) => text.toLocaleLowerCase().includes(skill.toLocaleLowerCase()));
  return {
    full_name: textLines.find((line) => !line.includes("@") && !/\d{5,}/.test(line) && line.length < 80) || "",
    email, phone, target_roles: [], skills, education: [], experience: [], projects: [], certifications: [],
  };
}

export function ResumeView({ data, demo, onResume, onApplyResume, notify }: ViewProps) {
  const [text, setText] = useState("");
  const [fields, setFields] = useState<ResumeFields | null>(data.resume?.extracted_data || null);
  const [selected, setSelected] = useState<string[]>([]);
  const [selectedRoles, setSelectedRoles] = useState<string[]>([]);
  const [includeName, setIncludeName] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    setFields(data.resume?.extracted_data || null);
    setSelected(data.resume?.extracted_data.skills || []);
    setSelectedRoles([]);
  }, [data.resume]);

  function changeList(key: keyof ResumeFields, value: string) {
    setFields((current) => current ? { ...current, [key]: lines(value) } : current);
  }
  function changeText(key: "full_name" | "email" | "phone", value: string) {
    setFields((current) => current ? { ...current, [key]: value } : current);
  }

  async function upload(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    setBusy(true); setError("");
    try {
      let resume: ResumeSnapshot;
      if (demo) {
        if (text.trim().length < 40) throw new Error("Paste at least 40 characters of resume text.");
        const extracted_data = suggest(text);
        const now = new Date().toISOString();
        resume = {
          id: crypto.randomUUID(), filename: "Pasted resume text.txt", media_type: "text/plain", byte_size: new TextEncoder().encode(text).length,
          sha256: `demo-${crypto.randomUUID()}`, extraction_method: "heuristic", extracted_data, reviewed_at: null, created_at: now, updated_at: now,
        };
      } else {
        const file = (event.currentTarget.elements.namedItem("resume") as HTMLInputElement)?.files?.[0];
        if (file) {
          const form = new FormData(); form.set("file", file);
          resume = await api<ResumeSnapshot>("/me/resume", "POST", form);
        } else {
          if (text.trim().length < 40) throw new Error("Choose a PDF or DOCX, or paste at least 40 characters of resume text.");
          resume = await api<ResumeSnapshot>("/me/resume/text", "POST", { text });
        }
      }
      onResume(resume);
      setText("");
      notify("Resume added. Check each suggestion before using it.");
    } catch (reason) { setError(messageOf(reason)); }
    finally { setBusy(false); }
  }

  async function saveReview() {
    if (!fields || !data.resume || busy) return;
    setBusy(true); setError("");
    try {
      const resume = demo ? { ...data.resume, extracted_data: fields, reviewed_at: new Date().toISOString(), updated_at: new Date().toISOString() }
        : await api<ResumeSnapshot>("/me/resume/review", "POST", { expected_sha256: data.resume.sha256, fields });
      onResume(resume); notify("Reviewed resume details saved.");
    } catch (reason) { setError(messageOf(reason)); }
    finally { setBusy(false); }
  }

  async function removeResume() {
    if (!data.resume || !window.confirm("Remove this resume and its extracted details?")) return;
    setBusy(true); setError("");
    try {
      if (!demo) await api<void>("/me/resume", "DELETE");
      onResume(null); setFields(null); notify("Resume removed.");
    } catch (reason) { setError(messageOf(reason)); }
    finally { setBusy(false); }
  }

  return <div className="resume-page">
    <div className="page-heading"><div><div className="eyebrow">YOUR FOUNDATION</div><h1>Resume</h1><p>Bring your experience in, review what we find, and choose what belongs on your profile.</p></div></div>
    <section className="resume-card">
      <h2>{data.resume ? "Replace your resume" : "Add a resume"}</h2>
      <p>PDF or DOCX, up to 5 MB. You can also paste the text from your resume.</p>
      <form className="editor-form resume-upload" onSubmit={upload}>
        {!demo && <label>Resume file<input name="resume" type="file" accept=".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document" /></label>}
        <label>Paste resume text<textarea value={text} onChange={(event) => setText(event.target.value)} rows={5} placeholder="Paste your resume text here" /></label>
        <button className="button primary" disabled={busy}>{busy ? "Working…" : "Review resume"}</button>
      </form>
      {demo && <small className="resume-note">Example mode saves in this browser only. File uploads are available on a personal account.</small>}
    </section>

    {fields && data.resume && <section className="resume-card">
      <div className="resume-card-heading"><div><h2>Review extracted details</h2><p>Suggestions are heuristic. Edit them before saving or adding skills to your profile.</p></div><button type="button" className="button plain" onClick={removeResume} disabled={busy}>Remove resume</button></div>
      <div className="resume-file"><Icon name="folder" size={18} /><span><b>{data.resume.filename}</b><small>{Math.max(1, Math.round(data.resume.byte_size / 1024))} KB · Suggestions {data.resume.reviewed_at ? "reviewed" : "not reviewed"}</small></span>{!demo && <a href="/api/v1/me/resume/file">Download</a>}</div>
      <div className="resume-fields">
        <label>Name<input value={fields.full_name} maxLength={120} onChange={(e) => changeText("full_name", e.target.value)} /></label>
        <label>Email<input type="email" value={fields.email} maxLength={254} onChange={(e) => changeText("email", e.target.value)} /></label>
        <label>Phone<input value={fields.phone} maxLength={40} onChange={(e) => changeText("phone", e.target.value)} /></label>
        <label>Target roles<textarea rows={3} value={fields.target_roles.join("\n")} onChange={(e) => changeList("target_roles", e.target.value)} /></label>
        <label>Skills<textarea rows={4} value={fields.skills.join("\n")} onChange={(e) => { changeList("skills", e.target.value); setSelected(lines(e.target.value)); }} /><small>One per line. You choose before adding skills to your profile.</small></label>
        {fieldLabels.map(([key, label]) => <label key={key}>{label}<textarea rows={3} value={fields[key].join("\n")} onChange={(e) => changeList(key, e.target.value)} placeholder="One item per line" /></label>)}
      </div>
      {error && <p className="form-error" role="alert">{error}</p>}
      <div className="resume-actions"><button type="button" className="button primary" onClick={saveReview} disabled={busy}>Save reviewed details</button><button type="button" className="button secondary" onClick={() => onApplyResume(selected, selectedRoles, includeName ? fields.full_name : undefined)} disabled={busy || (!selected.length && !selectedRoles.length && !includeName)}>Add selected details to profile</button></div>
      <div className="resume-select"><b>Profile updates</b>{fields.skills.map((skill) => <label key={skill}><input type="checkbox" checked={selected.includes(skill)} onChange={(e) => setSelected((current) => e.target.checked ? [...current, skill] : current.filter((item) => item !== skill))} />{skill}</label>)}
        {fields.target_roles.map((role) => <label key={role}><input type="checkbox" checked={selectedRoles.includes(role)} onChange={(e) => setSelectedRoles((current) => e.target.checked ? [...current, role] : current.filter((item) => item !== role))} />Target role: {role}</label>)}
        {fields.full_name && fields.full_name !== data.profile.full_name && <label><input type="checkbox" checked={includeName} onChange={(e) => setIncludeName(e.target.checked)} />Use suggested name: {fields.full_name}</label>}
      </div>
    </section>}
    {error && !fields && <p className="form-error" role="alert">{error}</p>}
  </div>;
}
