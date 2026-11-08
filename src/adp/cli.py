"""`adp` command line."""

import argparse
import sys
from datetime import date
from pathlib import Path

from adp.bronze.lifecycle import apply_archive, load_retention, plan_archive, summarize
from adp.bronze.store import LocalStore, ObjectStore, S3Store
from adp.errors import AdpError
from adp.mapping.decision import ReviewItem, ReviewQueue
from adp.mapping.dictionary import load_dictionary
from adp.schema.registry import make_engine

DEFAULT_DICTIONARY = Path("config/semantic/canonical.yaml")


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
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (AdpError, ValueError) as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
