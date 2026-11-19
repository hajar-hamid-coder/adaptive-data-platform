import { Badge, Empty, PageHeader } from "../components/ui";

/** Consultative screen (H-33): the API exposes no review-queue route, so nothing is invented. */
export function Review() {
  return (
    <>
      <PageHeader title="Revue du mapping" sub="Propositions d'auto-mapping avec confiance, méthode et alternatives." />
      <div className="alert ok" role="note">
        <span className="txt">Les propositions LLM sont validées par schéma et ne sont jamais exécutées sans décision humaine. Seuls les noms de colonnes et des échantillons anonymisés sont envoyés.</span>
      </div>
      <section className="card" aria-labelledby="h-q">
        <header>
          <h2 id="h-q">File de revue</h2>
          <Badge tone="info">Indisponible via l'API</Badge>
        </header>
        <Empty>
          La file de revue n'est pas exposée par l'API en lecture seule : aucune proposition n'est affichée.
          <br />
          <span className="small">
            Consultez-la et décidez en ligne de commande : <code>adp review list --status pending</code>, puis{" "}
            <code>adp review approve ID --by NOM</code> ou <code>adp review reject ID --by NOM</code>.
          </span>
        </Empty>
      </section>
    </>
  );
}
