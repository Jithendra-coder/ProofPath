"use client";

import { StatusPanel } from "./components/ui";

export default function ErrorPage({ reset }: { reset: () => void }) {
  return (
    <StatusPanel
      title="Let’s give that another try."
      message="This page couldn’t open. Your saved work is still here."
      action="Try again"
      onAction={reset}
    />
  );
}
