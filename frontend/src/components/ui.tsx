import type { UseQueryResult } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { errorMessage } from "../api/client";
import type { Tone } from "../lib";
import { fmtInt, fmtScore, scoreTone } from "../lib";

export function Badge({ tone, children }: { tone: Tone; children: ReactNode }) {
  return <span className={`badge ${tone}`}>{children}</span>;
}

/** Track + fill + numeric value + texture: the state never relies on colour alone. */
export function ScoreBar({ value, label = "Score", unit = "sur 100" }: { value: number | null; label?: string; unit?: string }) {
  if (value === null) return <span className="muted">n/a</span>;
  const tone = scoreTone(value);
  return (
    <div className={`bar ${tone === "ok" ? "" : tone}`}>
      <div className="track" role="img" aria-label={`${label} ${fmtScore(value)} ${unit}`}>
        <div className="fill" style={{ width: `${Math.max(0, Math.min(100, value))}%` }} />
      </div>
      <b>{fmtScore(value)}</b>
    </div>
  );
}

export function Kpi({ label, value, hint }: { label: string; value: ReactNode; hint?: ReactNode }) {
  return (
    <div className="card kpi">
      <div className="label">{label}</div>
      <div className="value">{value}</div>
      {hint && <div className="hint muted">{hint}</div>}
    </div>
  );
}

export function PageHeader({ title, sub }: { title: string; sub: string }) {
  return (
    <div>
      <h1>{title}</h1>
      <p className="muted small mt-1">{sub}</p>
    </div>
  );
}

export function TableRegion({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="table-wrap" role="region" aria-label={label} tabIndex={0}>
      {children}
    </div>
  );
}

export function Pager({ offset, limit, total, onChange, noun }: { offset: number; limit: number; total: number; onChange: (o: number) => void; noun: string }) {
  const from = total === 0 ? 0 : offset + 1;
  const to = Math.min(offset + limit, total);
  return (
    <nav className="pager" aria-label={`Pagination ${noun}`}>
      <span className="muted small" aria-live="polite">
        {from} à {to} sur {fmtInt(total)}
      </span>
      <span className="flex gap-2">
        <button className="btn small" type="button" disabled={offset <= 0} onClick={() => onChange(Math.max(0, offset - limit))}>
          Précédent
        </button>
        <button className="btn small" type="button" disabled={offset + limit >= total} onClick={() => onChange(offset + limit)}>
          Suivant
        </button>
      </span>
    </nav>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

/** Loading (skeleton + aria-live), error (role=alert + retry), empty, then content. */
export function Async<T>({ query, isEmpty, empty, children }: { query: UseQueryResult<T, unknown>; isEmpty?: (d: T) => boolean; empty: ReactNode; children: (d: T) => ReactNode }) {
  if (query.isPending)
    return (
      <div role="status" aria-live="polite" aria-busy="true">
        <span className="sr-only">Chargement des données…</span>
        <div className="skeleton w-4/5" />
        <div className="skeleton w-3/5" />
        <div className="skeleton w-[70%]" />
      </div>
    );
  if (query.isError)
    return (
      <div className="alert error" role="alert">
        <span className="txt">{errorMessage(query.error)}</span>
        <button className="btn small" type="button" onClick={() => void query.refetch()}>
          Réessayer
        </button>
      </div>
    );
  if (isEmpty?.(query.data)) return <Empty>{empty}</Empty>;
  return <>{children(query.data)}</>;
}

export function SelectField({ id, label, value, options, onChange }: { id: string; label: string; value: string; options: { value: string; label: string }[]; onChange: (v: string) => void }) {
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      <select id={id} value={value} onChange={(e) => onChange(e.target.value)}>
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </div>
  );
}
