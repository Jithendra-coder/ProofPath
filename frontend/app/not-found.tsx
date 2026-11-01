import { StatusPanel } from "./components/ui";

export default function NotFound() {
  return (
    <StatusPanel
      title="A little off the beaten path."
      message="We couldn’t find this page. Let’s get you back to something good."
      action="Back to Proofpath"
      href="/"
    />
  );
}
