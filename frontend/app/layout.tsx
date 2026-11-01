import type { Metadata } from "next";
import "./globals.css";
import "./proofpath.css";

export const metadata: Metadata = {
  title: "Proofpath — Let your work lead the way",
  description:
    "You’re more than a résumé. Showcase your work, connect your skills to real evidence, and find your next opportunity on Proofpath.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" data-scroll-behavior="smooth">
      <body>
        <a className="skip-link" href="#main-content">
          Skip to content
        </a>
        {children}
      </body>
    </html>
  );
}
