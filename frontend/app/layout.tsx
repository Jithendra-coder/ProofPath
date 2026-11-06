import type { Metadata } from "next";
import "./globals.css";
import "./proofpath.css";

export const metadata: Metadata = {
  title: "Proofpath — Let your work lead the way",
  description:
    "Build a professional profile, connect skills to project evidence, and share your work on your terms with Proofpath.",
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
