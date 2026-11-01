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
export type Job = {
  id: string;
  title: string;
  organization_name: string;
  description: string;
  competency_requirements: { competency_name: string; target_level: string }[];
  location?: string;
  salary?: string;
  category?: string;
  color?: string;
};
export type Application = {
  cover_note?: string;
  id: string;
  job_id: string;
  job_title: string;
  organization_name: string;
  status: string;
  submitted_at: string;
  version: number;
};
export type WorkspaceData = {
  profile: Profile;
  projects: Project[];
  jobs: Job[];
  saved: string[];
  applications: Application[];
  public_handle: string | null;
  published_at: string | null;
};

export const exampleJobs: Job[] = [
  {
    id: "form-and-field",
    title: "Senior Product Designer",
    organization_name: "Form & Field",
    description:
      "Help us make everyday work feel a little more human. You’ll own product experiences from the first conversation to the final detail, working closely with a small team of designers and engineers. We value thoughtful decisions, clear communication, and work that speaks for itself.",
    competency_requirements: [
      { competency_name: "Product design", target_level: "Advanced" },
      { competency_name: "Figma", target_level: "Advanced" },
      { competency_name: "Design systems", target_level: "Intermediate" },
    ],
    location: "Remote",
    salary: "$120k – $160k",
    category: "Design",
    color: "peach",
  },
  {
    id: "layers",
    title: "Design Engineer",
    organization_name: "Layers",
    description:
      "Bridge design and code to build a beautifully crafted collaboration product. You’ll prototype ideas, evolve our component library, and bring care to the interactions that make our product feel special.",
    competency_requirements: [
      { competency_name: "React", target_level: "Advanced" },
      { competency_name: "UI design", target_level: "Advanced" },
      { competency_name: "Accessibility", target_level: "Intermediate" },
    ],
    location: "Remote",
    salary: "$130k – $175k",
    category: "Engineering",
    color: "lavender",
  },
  {
    id: "orbit",
    title: "Product Designer",
    organization_name: "Orbit Studio",
    description:
      "Design useful, approachable tools for independent businesses. Bring curiosity, a strong portfolio, and the ability to turn complex problems into simple experiences.",
    competency_requirements: [
      { competency_name: "User research", target_level: "Intermediate" },
      { competency_name: "Prototyping", target_level: "Intermediate" },
    ],
    location: "New York · Hybrid",
    salary: "$100k – $140k",
    category: "Design",
    color: "blue",
  },
  {
    id: "northstar",
    title: "Frontend Engineer",
    organization_name: "Northstar",
    description:
      "Build accessible, high-quality web experiences with a team that cares deeply about craft. You’ll work across React, TypeScript, and our growing design system.",
    competency_requirements: [
      { competency_name: "React", target_level: "Advanced" },
      { competency_name: "TypeScript", target_level: "Advanced" },
    ],
    location: "Remote",
    salary: "$115k – $155k",
    category: "Engineering",
    color: "mint",
  },
  {
    id: "common",
    title: "UX Researcher",
    organization_name: "Common Ground",
    description:
      "Help us understand the people behind the numbers. Lead discovery studies, share clear insights, and partner with product teams to build experiences that matter.",
    competency_requirements: [
      { competency_name: "User research", target_level: "Advanced" },
      { competency_name: "Communication", target_level: "Advanced" },
    ],
    location: "London · Hybrid",
    salary: "£65k – £85k",
    category: "Research",
    color: "yellow",
  },
];

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
  jobs: exampleJobs,
  saved: ["layers"],
  applications: [
    {
      id: "example-a",
      job_id: "orbit",
      job_title: "Product Designer",
      organization_name: "Orbit Studio",
      status: "in_review",
      submitted_at: "2026-10-28T10:00:00Z",
      version: 1,
    },
    {
      id: "example-b",
      job_id: "northstar",
      job_title: "Frontend Engineer",
      organization_name: "Northstar",
      status: "submitted",
      submitted_at: "2026-10-30T10:00:00Z",
      version: 1,
    },
  ],
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
  jobs: [],
  saved: [],
  applications: [],
  public_handle: null,
  published_at: null,
};

export function matchingJobs(
  jobs: Job[],
  query: string,
  category: string,
  remote: boolean,
  savedOnly: boolean,
  saved: string[],
) {
  const term = query.trim().toLowerCase();
  return jobs.filter(
    (job) =>
      (!term ||
        `${job.title} ${job.organization_name} ${job.competency_requirements.map((item) => item.competency_name).join(" ")}`
          .toLowerCase()
          .includes(term)) &&
      (category === "All roles" || job.category === category) &&
      (!remote || job.location === "Remote") &&
      (!savedOnly || saved.includes(job.id)),
  );
}

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
    if (
      typeof data.profile?.full_name !== "string" ||
      !strings(data.profile.skills) ||
      !strings(data.profile.target_roles) ||
      !strings(data.saved) ||
      !Array.isArray(data.projects) ||
      !Array.isArray(data.applications)
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
    if (
      !data.applications.every(
        (a: Application) =>
          [
            a.id,
            a.job_id,
            a.job_title,
            a.organization_name,
            a.status,
            a.submitted_at,
          ].every((value) => typeof value === "string") &&
          (a.cover_note === undefined || typeof a.cover_note === "string"),
      )
    )
      return structuredClone(exampleData);
    return {
      ...structuredClone(exampleData),
      profile: data.profile,
      projects: data.projects,
      saved: data.saved,
      applications: data.applications,
    };
  } catch {
    return structuredClone(exampleData);
  }
}
