import { useEffect, useState } from "react";
import { API_BASE } from "../api/client";
import { useQuarantine, useReport, useScores, useSources } from "../api/hooks";
import { Async, Badge, Empty, Kpi, PageHeader, Pager, ScoreBar, SelectField, TableRegion } from "../components/ui";
import { QSTATUS, SEVERITY, fmtInt, fmtScore, scoreTone } from "../lib";

const PAGE = 10;

export function Quality() {
  const sources = useSources();
  const [source, setSource] = useState<string>();
  const [run, setRun] = useState<string>();
  const [offset, setOffset] = useState(0);
  const src = source ?? sources.data?.[0];
  const scores = useScores(src);
  const runId = run && scores.data?.items.some((s) => s.run_id === run) ? run : scores.data?.items[0]?.run_id;
  const sel = scores.data?.items.find((s) => s.run_id === runId);
  const report = useReport(runId);
  const quarantine = useQuarantine(src, offset, PAGE);
  useEffect(() => setOffset(0), [src]);

  return (
    <>
      <PageHeader title="Qualité des données" sub="Scores, règles en échec, quarantaine et rapports. Source : GET /quality/*" />
      <Async query={sources} isEmpty={(s) => s.length === 0} empty={<>Aucune source avec score qualité.<br /><span className="small">Exécutez une promotion pour produire un score.</span></>}>
        {(list) => (
          <>
            <div className="filters card">
              <SelectField id="src" label="Source" value={src ?? ""} options={list.map((s) => ({ value: s, label: s }))} onChange={(v) => { setSource(v); setRun(undefined); }} />
              <SelectField id="run" label="Run" value={runId ?? ""} options={(scores.data?.items ?? []).map((s) => ({ value: s.run_id, label: s.run_id }))} onChange={setRun} />
            </div>
            <Async query={scores} isEmpty={(p) => p.items.length === 0} empty="Aucun score pour cette source.">
              {() =>
                sel && (
                  <section className="cards" aria-label="Scores">
                    <Kpi label={`Score global (${sel.source_name})`} value={fmtScore(sel.score)} hint={scoreTone(sel.score) === "ok" ? <Badge tone="ok">Au-dessus du seuil de 90</Badge> : <Badge tone="warn">Sous le seuil de 90</Badge>} />
                    <Kpi label="Complétude" value={fmtScore(sel.completeness)} />
                    <Kpi label="Validité" value={fmtScore(sel.validity)} />
                    <Kpi label="Unicité" value={fmtScore(sel.uniqueness)} />
                  </section>
                )
              }
            </Async>
            <section className="cards two">
              <div className="card">
                <header><h2>Score par colonne</h2><span className="muted small">Seuil de promotion : 90</span></header>
                <Async query={report} isEmpty={(r) => r.columns.length === 0} empty="Aucune colonne dans le rapport.">
                  {(r) => (
                    <TableRegion label="Scores par colonne">
                      <table>
                        <caption className="small">Texte, barre et valeur : l'état ne dépend pas de la couleur</caption>
                        <thead><tr><th scope="col">Colonne</th><th scope="col">Score</th><th scope="col" className="num">Lignes en échec</th></tr></thead>
                        <tbody>
                          {r.columns.map((c) => (
                            <tr key={c.column}>
                              <th scope="row"><code>{c.column}</code></th>
                              <td><ScoreBar value={c.score} /></td>
                              <td className="num">{fmtInt(c.rows_failed)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </TableRegion>
                  )}
                </Async>
              </div>
              <div className="card">
                <header><h2>Règles en échec</h2><span className="muted small">Rapport {runId ?? ""}</span></header>
                <Async query={report} isEmpty={(r) => r.failed_rules.length === 0} empty="Aucune règle en échec sur ce run.">
                  {(r) => (
                    <TableRegion label="Règles en échec">
                      <table>
                        <caption className="small">Règles en échec du run {r.run_id}</caption>
                        <thead><tr><th scope="col">Règle</th><th scope="col">Sévérité</th><th scope="col" className="num">Lignes</th></tr></thead>
                        <tbody>
                          {r.failed_rules.map((f) => {
                            const sv = SEVERITY[f.severity] ?? { tone: "info" as const, label: f.severity };
                            return (
                              <tr key={f.rule_id}>
                                <th scope="row"><code>{f.rule_id}</code>{f.column && <><br /><span className="muted small"><code>{f.column}</code></span></>}</th>
                                <td><Badge tone={sv.tone}>{sv.label}</Badge></td>
                                <td className="num">{fmtInt(f.failed_count)}</td>
                              </tr>
                            );
                          })}
                        </tbody>
                      </table>
                    </TableRegion>
                  )}
                </Async>
                {runId && <p className="mt-3"><a href={`${API_BASE}/quality/reports/${encodeURIComponent(runId)}`} target="_blank" rel="noreferrer">Ouvrir le rapport JSON du run</a></p>}
              </div>
            </section>
            <section className="card" aria-labelledby="h-q">
              <header><h2 id="h-q">Quarantaine</h2><span className="muted small">Index et motifs seulement, jamais de valeurs brutes</span></header>
              <Async query={quarantine} isEmpty={(p) => p.total === 0} empty="Aucune ligne en quarantaine pour cette source.">
                {(p) => (
                  <>
                    <TableRegion label="Lignes en quarantaine">
                      <table>
                        <caption className="small">Lignes isolées de {src} (identifiant de lot abrégé)</caption>
                        <thead><tr><th scope="col">Lot</th><th scope="col" className="num">Ligne</th><th scope="col">Statut</th><th scope="col">Niveau</th><th scope="col">Motif</th></tr></thead>
                        <tbody>
                          {p.items.map((q) => {
                            const st = QSTATUS[q.status] ?? { tone: "info" as const, label: q.status };
                            return (
                              <tr key={`${q.batch_id}-${q.row}`}>
                                <td><code title={q.batch_id}>{q.batch_id.slice(0, 12)}</code></td>
                                <td className="num">{q.row}</td>
                                <td><Badge tone={st.tone}>{st.label}</Badge></td>
                                <td>{q.batch_level}</td>
                                <td>{q.reasons.map((r) => <code key={r.rule_id}>{r.rule_id}</code>)}</td>
                              </tr>
                            );
                          })}
                        </tbody>
                      </table>
                    </TableRegion>
                    <Pager offset={p.offset} limit={p.limit} total={p.total} onChange={setOffset} noun="de la quarantaine" />
                  </>
                )}
              </Async>
            </section>
            {!src && <Empty>Sélectionnez une source.</Empty>}
          </>
        )}
      </Async>
    </>
  );
}
