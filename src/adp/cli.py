"""`adp` command line."""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from adp.bronze.lifecycle import apply_archive, load_retention, plan_archive, summarize
from adp.bronze.store import LocalStore, ObjectStore, S3Store
from adp.errors import AdpError
from adp.gold.contract import PRODUCTS_DIR, impact_matrix, load_products, render_bia_markdown
from adp.gold.views import build_product
from adp.mapping.decision import ReviewItem, ReviewQueue
from adp.mapping.dictionary import load_dictionary
from adp.orchestration.promote_asset import build_gold_store
from adp.quality.quarantine import QuarantineStore, load_quarantine_config
from adp.quality.rules import load_references, load_rules
from adp.schema.registry import make_engine

DEFAULT_DICTIONARY = Path("config/semantic/canonical.yaml")
QUALITY_DIR = Path("config/quality")


def _archive(args: argparse.Namespace) -> int:
    policy = load_retention(args.config)
    store: ObjectStore = LocalStore(args.store_root) if args.store_root else S3Store("bronze")
    today = date.fromisoformat(args.today) if args.today else date.today()
    moves = plan_archive(store, policy, today)
    per_source = summarize(moves)
    mode = "SIMULATION (aucune modification)" if args.dry_run else "exécution"
    print(f"Archivage Bronze au {today} : {mode}")
    for source, (batches, files) in per_source.items():
        print(f"  {source} : {batches} lot(s), {files} fichier(s)")
    print(f"Total : {sum(b for b, _ in per_source.values())} lot(s), {len(moves)} fichier(s)")
    if args.dry_run:
        for m in moves:
            print(f"  {m.src} -> {m.dst}")
        return 0
    res = apply_archive(store, moves)
    print(f"Déplacés : {res.moved}, déjà archivés : {res.already_done}")
    return 0


def _queue(args: argparse.Namespace, with_dictionary: bool = False) -> ReviewQueue:
    dictionary = load_dictionary(args.dictionary) if with_dictionary else None
    return ReviewQueue(make_engine(args.db), dictionary)


def _line(i: ReviewItem) -> str:
    """Names, scores and statuses only: no data value, no rationale, no URL."""
    target = i.final_target or i.target
    who = f" par {i.decided_by}" if i.decided_by else ""
    return (
        f"  #{i.id} [{i.status}] {i.source_name}.{i.source_col} -> {target}"
        f"  score={i.score:.2f} {i.method}/{i.origin} ({i.reason}){who}"
    )


def _review_list(args: argparse.Namespace) -> int:
    items = _queue(args).list(status=args.status, source_name=args.source)
    print(f"File de revue : {len(items)} élément(s)")
    for i in items:
        print(_line(i))
    return 0


def _review_approve(args: argparse.Namespace) -> int:
    queue = _queue(args, with_dictionary=args.target is not None)
    item = queue.approve(args.id, args.by, note=args.note, target=args.target)
    print(f"Élément #{item.id} approuvé par {item.decided_by} :")
    print(_line(item))
    return 0


def _review_reject(args: argparse.Namespace) -> int:
    item = _queue(args).reject(args.id, args.by, note=args.note)
    print(f"Élément #{item.id} rejeté par {item.decided_by}.")
    return 0


def _qstore(args: argparse.Namespace) -> QuarantineStore:
    store: ObjectStore = LocalStore(args.store_root) if args.store_root else S3Store("quarantine")
    return QuarantineStore(store)


def _quarantine_list(args: argparse.Namespace) -> int:
    qs = _qstore(args)
    batches = qs.list_batches(args.source)
    print(f"Quarantaine {args.source} : {len(batches)} lot(s)")
    for b in batches:
        st = qs.state(args.source, b)
        c = st.counts
        print(f"  {b} [{st.status}] niveau={st.level} en quarantaine={c['quarantined']}"
              f" libérées={c['released']} rejouées={c['replayed']}")  # fmt: skip
    return 0


def _quarantine_release(args: argparse.Namespace) -> int:
    rows = [int(x) for x in args.rows.split(",")] if args.rows else None
    out = _qstore(args).release(args.source, args.batch_id, by=args.by, reason=args.reason,
                                rows=rows)  # fmt: skip
    print(f"{len(out)} ligne(s) libérée(s) par {args.by.strip()}.")
    return 0


def _quarantine_replay(args: argparse.Namespace) -> int:
    rules = load_rules(args.rules)
    refs = load_references(rules, args.references_dir)
    res = _qstore(args).replay(
        args.source, args.batch_id, rules, by=args.by, reason=args.reason, references=refs,
        as_of=date.fromisoformat(args.as_of) if args.as_of else None,
        config=load_quarantine_config(args.config),
    )  # fmt: skip
    print(f"Rejeu : {len(res.passed)} ligne(s) réintégrée(s), "
          f"{res.still_quarantined} toujours en quarantaine.")  # fmt: skip
    return 0


def _products_validate(args: argparse.Namespace) -> int:
    products = load_products(args.dir)
    print(f"{len(products)} data product(s) valide(s) :")
    for p in products:
        print(f"  {p.name} v{p.version} [{p.criticality.value}] SLA {p.sla.freshness_hours} h")
    return 0


def _products_bia(args: argparse.Namespace) -> int:
    md = render_bia_markdown(impact_matrix(load_products(args.dir)))
    args.output.write_text(md, encoding="utf-8")
    print(f"Analyse d'impact écrite dans {args.output}")
    return 0


def _gold_build(args: argparse.Namespace) -> int:
    catalog = {p.name: p for p in load_products()}
    if args.product and args.product not in catalog:
        raise ValueError(f"unknown data product '{args.product}'")
    gold = LocalStore(args.gold_dir) if args.gold_dir else build_gold_store()
    out = LocalStore(args.out_dir) if args.out_dir else gold
    for p in catalog.values():
        if args.product and p.name != args.product:
            continue
        b = build_product(p, gold, out, catalog)
        state = "créé" if b.created else "inchangé"
        print(f"{b.product} v{b.version} : {b.rows} ligne(s), {b.content_hash[:12]} ({state})")
    return 0


def _catalog_export(args: argparse.Namespace) -> int:
    from adp.catalog.openlineage import export_openlineage
    from adp.catalog.runtime import default_catalog

    text = json.dumps(export_openlineage(default_catalog()), indent=2, ensure_ascii=False)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
        print(f"Export OpenLineage-lite écrit dans {args.output}")
    else:
        print(text)
    return 0


def _api_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("adp.api.main:create_app", factory=True, host=args.host, port=args.port)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="adp")
    sub = parser.add_subparsers(dest="group", required=True)
    bronze = sub.add_parser("bronze").add_subparsers(dest="cmd", required=True)
    a = bronze.add_parser("archive", help="archiver les lots Bronze au-delà de la rétention")
    a.add_argument("--dry-run", action="store_true", help="afficher le plan sans rien modifier")
    a.add_argument("--config", type=Path, default=Path("config/retention.yaml"))
    a.add_argument("--store-root", type=Path, help="dossier local (sinon S3/MinIO via Settings)")
    a.add_argument("--today", help="date de référence YYYY-MM-DD (défaut : aujourd'hui)")
    a.set_defaults(func=_archive)

    review = sub.add_parser("review", help="revue humaine des mappings").add_subparsers(
        dest="cmd", required=True
    )
    db_help = "URL de la base de métadonnées (défaut : $ADP_REGISTRY_URL, sinon SQLite local)"
    ls = review.add_parser("list", help="lister la file de revue")
    ls.add_argument("--status", choices=["pending", "approved", "rejected", "superseded"])
    ls.add_argument("--source", help="filtrer sur une source")
    ls.add_argument("--db", help=db_help)
    ls.set_defaults(func=_review_list)
    for name, func, verb in (
        ("approve", _review_approve, "approuver"),
        ("reject", _review_reject, "rejeter"),
    ):
        p = review.add_parser(name, help=f"{verb} un élément en attente")
        p.add_argument("id", type=int)
        p.add_argument("--by", required=True, help="auteur de la décision (obligatoire)")
        p.add_argument("--note", help="commentaire (pas de donnée sensible)")
        p.add_argument("--db", help=db_help)
        if name == "approve":
            p.add_argument("--target", help="cible corrigée (champ du glossaire canonique)")
            p.add_argument("--dictionary", type=Path, default=DEFAULT_DICTIONARY)
        p.set_defaults(func=func)

    quarantine = sub.add_parser("quarantine", help="quarantaine qualité").add_subparsers(
        dest="cmd", required=True
    )
    store_help = "dossier local (sinon S3/MinIO, bucket quarantine)"
    ql = quarantine.add_parser("list", help="lister les lots en quarantaine d'une source")
    ql.add_argument("source")
    ql.add_argument("--store-root", type=Path, help=store_help)
    ql.set_defaults(func=_quarantine_list)
    for name, func in (("release", _quarantine_release), ("replay", _quarantine_replay)):
        p = quarantine.add_parser(name, help=f"{name} d'un lot en quarantaine")
        p.add_argument("source")
        p.add_argument("batch_id")
        p.add_argument("--by", required=True, help="auteur (obligatoire)")
        p.add_argument("--store-root", type=Path, help=store_help)
        if name == "release":
            p.add_argument("--reason", required=True, help="raison (obligatoire)")
            p.add_argument("--rows", help="indices séparés par des virgules (défaut : tous)")
        else:
            p.add_argument("--reason", default="replay", help="raison du rejeu")
            p.add_argument("--rules", type=Path, default=QUALITY_DIR / "population.yaml")
            p.add_argument("--references-dir", type=Path, default=QUALITY_DIR / "reference")
            p.add_argument("--config", type=Path, default=QUALITY_DIR / "quarantine.yaml")
            p.add_argument("--as-of", help="date de référence YYYY-MM-DD pour la fraîcheur")
        p.set_defaults(func=func)

    products = sub.add_parser("products", help="contrats de data products").add_subparsers(
        dest="cmd", required=True
    )
    pv = products.add_parser("validate", help="valider config/products/*.yaml")
    pv.add_argument("--dir", type=Path, default=PRODUCTS_DIR)
    pv.set_defaults(func=_products_validate)
    pb = products.add_parser("bia", help="générer la matrice d'impact (docs/bia.md)")
    pb.add_argument("--dir", type=Path, default=PRODUCTS_DIR)
    pb.add_argument("--output", type=Path, default=Path("docs/bia.md"))
    pb.set_defaults(func=_products_bia)
    gold = sub.add_parser("gold", help="vues et indicateurs Gold").add_subparsers(
        dest="cmd", required=True
    )
    gb = gold.add_parser("build", help="construire les data products (Parquet sous gold/)")
    gb.add_argument("product", nargs="?", help="produit (défaut : tous)")
    gb.add_argument("--gold-dir", type=Path, help="racine du store gold_ready (défaut : config)")
    gb.add_argument("--out-dir", type=Path, help="racine de sortie (défaut : même store)")
    gb.set_defaults(func=_gold_build)
    cat = sub.add_parser("catalog", help="catalogue et lignage").add_subparsers(
        dest="cmd", required=True
    )
    ce = cat.add_parser("export", help="exporter le lignage (OpenLineage-lite JSON)")
    ce.add_argument("--output", type=Path, help="fichier de sortie (défaut : stdout)")
    ce.set_defaults(func=_catalog_export)
    api = sub.add_parser("api", help="API HTTP en lecture seule").add_subparsers(
        dest="cmd", required=True
    )
    srv = api.add_parser("serve", help="lancer l'API (uvicorn)")
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=8000)
    srv.set_defaults(func=_api_serve)
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (AdpError, ValueError) as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
