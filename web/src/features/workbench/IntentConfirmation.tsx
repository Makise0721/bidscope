interface ChipProps {
  value: string;
}

function Chip({ value }: ChipProps) {
  return <span className="chip">{value}</span>;
}

interface IntentConfirmationProps {
  runId: string;
  onApprove: () => void;
}

export function IntentConfirmation({ onApprove }: IntentConfirmationProps) {
  return (
    <div className="confirmation" role="region" aria-label="confirm intent">
      <h2>Confirm</h2>
      <p className="confirmation-summary">
        Weekly digest for <Chip value="四川" />, <Chip value="重庆" /> about{" "}
        <Chip value="智算中心" />, <Chip value="服务器" /> with budget{" "}
        <Chip value="≥500万" />.
      </p>
      <button type="button" className="icon-button" onClick={onApprove}>
        Approve
      </button>
    </div>
  );
}
