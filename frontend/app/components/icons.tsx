import type { CSSProperties } from "react";

const paths = {
  arrow: "M5 12h14m-6-6 6 6-6 6",
  upRight: "M7 17 17 7M7 7h10v10",
  grid: "M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z",
  briefcase: "M8 6V4h8v2M3 7h18v13H3zM3 11l9 4 9-4M10 13v3h4v-3",
  folder: "M3 5h7l2 3h9v12H3z",
  user: "M16 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0M4 21v-3a8 8 0 0 1 16 0v3",
  users:
    "M15 7a3 3 0 1 1-6 0 3 3 0 0 1 6 0M5 21v-3a7 7 0 0 1 14 0v3M19 4a3 3 0 0 1 0 6M21 14a5 5 0 0 1 2 4M5 4a3 3 0 0 0 0 6M3 14a5 5 0 0 0-2 4",
  award:
    "m12 2 3 2 4 1 1 4 2 3-2 3-1 4-4 1-3 2-3-2-4-1-1-4-2-3 2-3 1-4 4-1zM8 12l3 3 5-6",
  search: "M17 10a7 7 0 1 1-14 0 7 7 0 0 1 14 0m-2 5 6 6",
  bell: "M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4",
  bookmark: "M6 3h12v18l-6-4-6 4z",
  check: "m5 12 4 4L19 6",
  plus: "M12 5v14M5 12h14",
  close: "m6 6 12 12M6 18 18 6",
  chevron: "m9 5 7 7-7 7",
  down: "m6 9 6 6 6-6",
  location:
    "M19 9c0 5-7 12-7 12S5 14 5 9a7 7 0 1 1 14 0M14 9a2 2 0 1 1-4 0 2 2 0 0 1 4 0",
  clock: "M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0M12 7v5l3 2",
  link: "m10 13 4-4M8 16l-1 1a4 4 0 0 1-6-6l4-4a4 4 0 0 1 6 0M16 8l1-1a4 4 0 0 1 6 6l-4 4a4 4 0 0 1-6 0",
  settings:
    "m12 2 2 3 4 1 1 4 3 2-3 2-1 4-4 1-2 3-2-3-4-1-1-4-3-2 3-2 1-4 4-1zM15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0",
  help: "M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0M9 9a3 3 0 0 1 6 0c0 2-3 2-3 5M12 17v.1",
  logout: "M9 3H3v18h6M9 12h12m-5-5 5 5-5 5",
  menu: "M4 6h16M4 12h16M4 18h16",
  edit: "m15 4 5 5M3 21l5-1L21 7l-5-5L3 15z",
  eye: "M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12M15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0",
  lock: "M7 10V7a5 5 0 0 1 10 0v3M5 10h14v11H5zM12 14v3",
  sparkle: "m12 2 3 7 7 3-7 3-3 7-3-7-7-3 7-3z",
  chart: "M4 20V10M10 20V4M16 20v-7M22 20V7",
  code: "m8 6-6 6 6 6M16 6l6 6-6 6M14 3l-4 18",
  globe:
    "M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0M3 12h18M12 3c5 5 5 13 0 18-5-5-5-13 0-18",
  mail: "M3 5h18v14H3zM3 5l9 8 9-8",
  filter: "M4 6h16M7 12h10M10 18h4",
  shield: "m12 2 9 4v6c0 6-9 10-9 10S3 18 3 12V6zM8 12l3 3 5-6",
} as const;

export type IconName = keyof typeof paths;
export function Icon({
  name,
  size = 20,
  className,
  style,
}: {
  name: IconName;
  size?: number;
  className?: string;
  style?: CSSProperties;
}) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.65"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      className={className}
      style={style}
    >
      <path d={paths[name]} />
    </svg>
  );
}

export function BrandMark() {
  return (
    <svg
      className="brand-mark"
      viewBox="0 0 32 32"
      fill="none"
      aria-hidden="true"
    >
      <path d="M5 28V4h13a9 9 0 0 1 0 18h-5v6H5Z" fill="currentColor" />
      <path
        d="m12 13 4 4 8-9"
        stroke="white"
        strokeWidth="3"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
