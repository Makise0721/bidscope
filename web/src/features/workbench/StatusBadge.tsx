import type { RunPhase } from "./Workbench";

interface StatusBadgeProps {
  phase: RunPhase;
}

export function StatusBadge({ phase }: StatusBadgeProps) {
  if (phase === "idle") return null;
  return (
    <p className={`status status-${phase}`} role="status">
      {labelFor(phase)}
    </p>
  );
}

function labelFor(phase: RunPhase): string {
  switch (phase) {
    case "loading":
      return "Searching…";
    case "auth_needed":
      return "Authentication required";
    case "awaiting_confirmation":
      return "Awaiting confirmation";
    case "running":
      return "Running…";
    case "completed":
      return "Completed";
    case "failed":
      return "Failed";
    default:
      return "";
  }
}
