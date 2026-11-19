import { useState } from "react";
import { useSchemaDiff, useSchemaVersions, useSources } from "../api/hooks";
import type { SchemaChange } from "../api/types";
import { Async, Badge, PageHeader, SelectField, TableRegion } from "../components/ui";
import { IMPACT, SIGNS, impactOf, isoMinute } from "../lib";

const SIGN_CLASS: Record<string, string> = { add_column: "add", drop_column: "del", rename_column: "ren" };

function describe(c: SchemaChange): string {
  const d = c.detail;
  const s = (k: string): string => (d[k] === null || d[k] === undefined ? "" : String(d[k]));
  switch (c.change_type) {
    case "add_column":
      return [s("type"), d["nullable"] ? "nullable" : ""].filter(Boolean).join(", ");
    case "drop_column":
      return s("type") ? `Colonne de type ${s("type")} supprimée.` : "Colonne supprimée.";
    case "rename_column":
      return `${s("from")} vers ${s("to")}${s("score") ? ` (score ${s("score")})` : ""}`;
    default:
      return [s("from") && `${s("from")} vers ${s("to")}`, s("rule_id") && `règle ${s("rule_id")}`].filter(Boolean).join(", ") || s("type");
  }
}

export function Schemas() {
  const sources = useSources();
  const [source, setSource] = useState<string>();
  const [from, setFrom] = useState<string>();
  const [to, setTo] = useState<string>();
  const src = source ?? sources.data?.[0];
  const versions = useSchemaVersions(src);
  const nums = (versions.data?.items ?? []).map((v) => v.version).sort((a, b) => a - b);
  const last = nums.at(-1);
  const toV = to && nums.includes(Number(to)) ? to : last?.toString();
  const fromV = from && nums.includes(Number(from)) ? from : (nums.length > 1 ? nums.at(-2) : undefined)?.toString();
  const diff = useSchemaDiff(src, fromV ? Number(fromV) : undefined, toV ? Number(toV) : undefined);
  const opts = nums.map((n) => ({ value: String(n), label: `v${n}` }));

  return (
    <>
      <PageHeader title="Schémas" sub="Versions et différences avec impact COMPATIBLE, REVIEW ou BLOCKING. Source : GET /schemas/{source}" />
      <Async query={sources} isEmpty={(s) => s.length === 0} empty="Aucune source connue.">
        {(list) => (
          <>
            <div className="filters card">
              <SelectField id="src" label="Source" value={src ?? ""} options={list.map((s) => ({ value: s, label: s }))} onChange={(v) => { setSource(v); setFrom(undefined); setTo(undefined); }} />
              <SelectField id="from" label="De" value={fromV ?? ""} options={opts} onChange={setFrom} />
              <SelectField id="to" label="Vers" value={toV ?? ""} options={opts} onChange={setTo} />
            </div>
            <section className="card" aria-labelledby="h-v">
              <header><h2 id="h-v">Versions du schéma</h2><span className="muted small">GET /schemas/{src}</span></header>
              <Async query={versions} isEmpty={(p) => p.items.length === 0} empty="Aucune version enregistrée.">
                {(p) => (
                  <TableRegion label="Versions">
                    <table>
                      <caption className="small">Versions de {src}, de la plus récente à la plus ancienne</caption>
                      <thead><tr><th scope="col">Version</th><th scope="col">Semver</th><th scope="col">Empreinte</th><th scope="col" className="num">Colonnes</th><th scope="col">Créée le</th><th scope="col">Statut</th></tr></thead>
                      <tbody>
                        {[...p.items].sort((a, b) => b.version - a.version).map((v) => (
                          <tr key={v.version}>
                            <th scope="row">v{v.version}</th>
                            <td>{v.semver}</td>
                            <td><code>{v.fingerprint.slice(0, 8)}</code></td>
                            <td className="num">{v.columns.length}</td>
                            <td>{isoMinute(v.created_at)}</td>
                            <td>{v.accepted_at ? <Badge tone="ok">Accepté</Badge> : <Badge tone="warn">En attente de revue</Badge>}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </TableRegion>
                )}
              </Async>
            </section>
            <section className="card" aria-labelledby="h-d">
              <header>
                <h2 id="h-d">{fromV && toV ? `Différences v${fromV} vers v${toV}` : "Différences"}</h2>
                <DiffImpact changes={diff.data?.items} />
              </header>
              {fromV === undefined ? (
                <div className="empty">Une seule version : aucune différence à comparer.</div>
              ) : (
                <Async query={diff} isEmpty={(p) => p.items.length === 0} empty="Aucun changement enregistré entre ces deux versions.">
                  {(p) => (
                    <>
                      <ul className="diff" aria-label="Changements">
                        {[...p.items].sort((a, b) => impactOf(b.detail).rank - impactOf(a.detail).rank).map((c) => {
                          const imp = impactOf(c.detail);
                          return (
                            <li key={`${c.change_type}-${c.column_name}`} className={SIGN_CLASS[c.change_type] ?? "typ"}>
                              <span className="sign" aria-hidden="true">{SIGNS[c.change_type] ?? "≠"}</span>
                              <div>
                                <b><code>{c.change_type}{c.column_name ? ` : ${c.column_name}` : ""}</code></b>
                                <br />
                                <span className="muted small">{describe(c)}</span>
                              </div>
                              <Badge tone={imp.tone}>{imp.label}</Badge>
                            </li>
                          );
                        })}
                      </ul>
                      <p className="small muted mt-3">Légende : + ajout, − suppression, ~ renommage, ≠ changement de type ou de format.</p>
                    </>
                  )}
                </Async>
              )}
            </section>
          </>
        )}
      </Async>
    </>
  );
}

function DiffImpact({ changes }: { changes: SchemaChange[] | undefined }) {
  if (!changes?.length) return null;
  const worst = changes.map((c) => impactOf(c.detail)).reduce((a, b) => (b.rank > a.rank ? b : a), IMPACT["compatible"]!);
  return <span>Impact global : <Badge tone={worst.tone}>{worst.label}</Badge></span>;
}
