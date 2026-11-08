"""`adp` command line."""

import argparse
import sys
from datetime import date
from pathlib import Path

from adp.bronze.lifecycle import apply_archive, load_retention, plan_archive, summarize
from adp.bronze.store import LocalStore, ObjectStore, S3Store
from adp.errors import AdpError


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
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (AdpError, ValueError) as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
