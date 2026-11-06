export type Profile = {
  full_name: string;
  target_roles: string[];
  skills: string[];
  status: "draft" | "complete";
  version: number;
};
export type Project = {
  id: string;
  name: string;
  problem_statement: string;
  contribution: string;
  technologies: string[];
  visibility: "private" | "public";
  version: number;
  artwork?: string;
};
export type ResumeFields = {
  full_name: string;
  email: string;
  phone: string;
  target_roles: string[];
  skills: string[];
  education: string[];
  experience: string[];
  projects: string[];
  certifications: string[];
};
export type ResumeSnapshot = {
  id: string;
  filename: string;
  media_type: string;
  byte_size: number;
  sha256: string;
  extraction_method: "heuristic";
  extracted_data: ResumeFields;
  reviewed_at: string | null;
  created_at: string;
  updated_at: string;
};
export type WorkspaceData = {
  profile: Profile;
  projects: Project[];
  resume: ResumeSnapshot | null;
  public_handle: string | null;
  published_at: string | null;
};

export const exampleData: WorkspaceData = {
  profile: {
    full_name: "Alex Morgan",
    target_roles: ["Product Designer"],
    skills: [
      "Product design",
      "Figma",
      "Design systems",
      "User research",
      "Prototyping",
      "React",
    ],
    status: "complete",
    version: 1,
  },
  projects: [
    {
      id: "folio",
      name: "Folio — Finance, made human",
      problem_statement:
        "Making personal finance feel less overwhelming and more empowering.",
      contribution:
        "Led research with 12 participants, mapped the core journeys, and designed an accessible mobile experience that helps people build healthier money habits.",
      technologies: ["Product design", "User research", "Figma"],
      visibility: "public",
      version: 1,
      artwork: "folio",
    },
    {
      id: "forma",
      name: "Forma design system",
      problem_statement:
        "A shared visual language for a product that’s growing fast.",
      contribution:
        "Built a component library and documented interaction patterns, accessibility guidelines, and design tokens to help the team ship with confidence.",
      technologies: ["Design systems", "React", "Figma"],
      visibility: "public",
      version: 1,
      artwork: "forma",
    },
    {
      id: "waypoint",
      name: "Waypoint — Find your somewhere",
      problem_statement:
        "Helping curious travelers discover places beyond the usual itinerary.",
      contribution:
        "Designed the discovery experience, from an early concept through interactive prototypes and usability testing.",
      technologies: ["Product design", "Prototyping"],
      visibility: "private",
      version: 1,
      artwork: "waypoint",
    },
  ],
  resume: null,
  public_handle: "alex-morgan",
  published_at: "2026-10-01T10:00:00Z",
};

export const emptyData: WorkspaceData = {
  profile: {
    full_name: "",
    target_roles: [],
    skills: [],
    status: "draft",
    version: 0,
  },
  projects: [],
  resume: null,
  public_handle: null,
  published_at: null,
};

export function skillEvidence(projects: Project[], skill: string) {
  return projects.filter((project) =>
    project.technologies.some(
      (technology) => technology.toLowerCase() === skill.toLowerCase(),
    ),
  );
}

export function parseDemoState(raw: string | null): WorkspaceData {
  if (!raw) return structuredClone(exampleData);
  try {
    const data = JSON.parse(raw);
    const strings = (value: unknown): value is string[] =>
      Array.isArray(value) && value.every((item) => typeof item === "string");
    const savedResume = data.resume;
    const resume = savedResume && typeof savedResume === "object" &&
      typeof savedResume.filename === "string" && typeof savedResume.sha256 === "string" &&
      savedResume.extracted_data && typeof savedResume.extracted_data.full_name === "string" &&
      typeof savedResume.extracted_data.email === "string" && typeof savedResume.extracted_data.phone === "string" &&
      ["target_roles", "skills", "education", "experience", "projects", "certifications"].every((key) => strings(savedResume.extracted_data[key]))
      ? savedResume as ResumeSnapshot : null;
    if (
      typeof data.profile?.full_name !== "string" ||
      !strings(data.profile.skills) ||
      !strings(data.profile.target_roles) ||
      !Array.isArray(data.projects)
    )
      return structuredClone(exampleData);
    if (
      !data.projects.every(
        (p: Project) =>
          typeof p.id === "string" &&
          typeof p.name === "string" &&
          typeof p.problem_statement === "string" &&
          typeof p.contribution === "string" &&
          strings(p.technologies) &&
          ["public", "private"].includes(p.visibility),
      )
    )
      return structuredClone(exampleData);
    return {
      ...structuredClone(exampleData),
      profile: data.profile,
      projects: data.projects,
      resume,
    };
  } catch {
    return structuredClone(exampleData);
  }
}
