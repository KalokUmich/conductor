import { formatDate } from "../../utils/format";

export function DateSeparator({ ts }: { ts: number }) {
  return (
    <div className="date-separator">
      <div className="date-line" />
      <span className="date-label">{formatDate(ts)}</span>
      <div className="date-line" />
    </div>
  );
}
