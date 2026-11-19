import { useSchemaVersions, useRuns } from "../api/hooks";
import type { Run } from "../api/types";
import { Async, Badge, Kpi, PageHeader, ScoreBar, TableRegion } from "../components/ui";
import { fmtInt, fmtScore, hhmm, isoMinute, latestBySource, runStatus } from "../lib";

function Stages({ run }: { run: Run }) {
  const t = hhmm(run.created_at);
  const isolated = run.rows_failed > 0;
  return (
    <ol className="stages" aria-label="Étapes du dernier run (déduites du score qualité)">
      <li className="stage ok">
        <b>Bronze</b>
        <Badge tone="ok">Ingéré</Badge>
      </li>
      <li className="arrow" aria-hidden="true" />
      <li className={`stage ${isolated ? "quarantine" : "ok"}`}>
        <b>Silver</b>
        {isolated ? <Badge tone="quarantine">{fmtInt(run.rows_failed)} lignes isolées</Badge> : <Badge tone="ok">Validé</Badge>}
      </li>
      <li className="arrow" aria-hidden="true" />
      <li className="stage ok">
        <b>Gold</b>
        <Badge tone="ok">Publié</Badge>
        <span className="t">{t} UTC</span>
      </li>
    </ol>
  );
}

function SchemaCell({ run }: { run: Run }) {
  const q = useSchemaVersions(run.source_name);
  const latest = q.data?.items.reduce((a, b) => (b.version > a.version ? b : a));
  const pending = latest && latest.accepted_at === null;
  return (
    <>
      {run.schema_version === null ? "n/a" : `v${run.schema_version}`}
      {pending && (
        <div>
          <Badge tone="warn">{`v${latest.version} à revoir`}</Badge>
        </div>
      )}
    </>
  );
}

export function Pipeline() {
  const runs = useRuns();
  return (
    <>
      <PageHeader title="Pipeline" sub="État du dernier run par source, du Bronze au Gold. Source : GET /runs" />
      <Async query={runs} isEmpty={(p) => p.items.length === 0} empty={<>Aucun run enregistré.<br /><span className="small">Lancez une ingestion ou chargez les données de démo.</span></>}>
        {(page) => {
          const latest = latestBySource(page.items);
          const scored = latest.filter((r) => r.score !== null);
          const avg = scored.length ? scored.reduce((s, r) => s + (r.score ?? 0), 0) / scored.length : null;
          const failed = latest.filter((r) => r.rows_failed > 0).length;
          return (
            <>
              <section className="cards" aria-label="Indicateurs clés">
                <Kpi label="Sources actives" value={latest.length} />
                <Kpi label="Runs enregistrés" value={fmtInt(page.total)} hint={`dont ${failed} source(s) avec lignes isolées (dernier run)`} />
                <Kpi label="Score qualité moyen" value={fmtScore(avg)} hint="dernier run de chaque source" />
                <Kpi label="Lignes en échec" value={fmtInt(latest.reduce((s, r) => s + r.rows_failed, 0))} hint="dernier run de chaque source" />
              </section>
              <section className="card" aria-labelledby="h-runs">
                <header>
                  <h2 id="h-runs">État des runs par source</h2>
                  <span className="muted small">Médaillon Bronze, Silver, Gold</span>
                </header>
                <TableRegion label="Tableau des runs, défilement horizontal possible">
                  <table>
                    <caption className="small">Dernier run de chaque source. Les étapes et le statut sont déduits des scores qualité (aucun journal de run persisté).</caption>
                    <thead>
                      <tr>
                        <th scope="col">Source</th>
                        <th scope="col">Étapes</th>
                        <th scope="col">Dernier run</th>
                        <th scope="col">Schéma</th>
                        <th scope="col">Score qualité</th>
                        <th scope="col">Statut</th>
                      </tr>
                    </thead>
                    <tbody>
                      {latest.map((r) => {
                        const st = runStatus(r);
                        return (
                          <tr key={r.source_name}>
                            <th scope="row"><code>{r.source_name}</code></th>
                            <td><Stages run={r} /></td>
                            <td>
                              <code>{r.run_id}</code>
                              <br />
                              <span className="muted">{isoMinute(r.created_at)}</span>
                            </td>
                            <td><SchemaCell run={r} /></td>
                            <td><ScoreBar value={r.score} /></td>
                            <td><Badge tone={st.tone}>{st.label}</Badge></td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </TableRegion>
                <p className="muted small mt-3">{latest.length} source(s) sur {fmtInt(page.total)} run(s).</p>
              </section>
            </>
          );
        }}
      </Async>
    </>
  );
}
