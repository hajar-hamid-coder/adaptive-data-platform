"""Build SYNTHETIC local demo stores so the read-only API (and the dashboard) show something.

Real chain through the repo functions: write_bronze -> schema gate -> run_silver_chain ->
promote_batch -> build_all, on tests/data/drift (v1 promoted, v2 compatible, v3 blocked at the
schema gate) and tests/data/gold (GDP). Idempotent: the stores are rebuilt only with --reset.
A random demo API key is generated at each run, printed ONCE and never written to disk.

Usage: uv run python scripts/demo_data.py [--root data/demo-store] [--reset]
"""

import argparse
import hashlib
import secrets
import shutil
import sys
from pathlib import Path

from sqlalchemy import create_engine

from adp.bronze.store import LocalStore
from adp.bronze.writer import write_bronze
from adp.connectors.files import read_file
from adp.gold.contract import load_products
from adp.gold.views import build_all
from adp.mapping.decision import ReviewQueue
from adp.mapping.dictionary import load_dictionary
from adp.mapping.llm import LLMMapper, LocalProvider
from adp.orchestration.promotion_gate import promote_batch
from adp.orchestration.schema_gate import read_bronze_table, run_schema_gate
from adp.orchestration.silver_chain import run_silver_chain
from adp.schema.events import EventBus
from adp.schema.policy import load_policy
from adp.schema.profile import profile_table
from adp.schema.registry import SchemaRegistry, metadata
from adp.sources.spec import SourceSpec

REPO = Path(__file__).resolve().parents[1]
MARKER = ".adp-demo"
# (source, csv, primary key, run id)
BATCHES = [
    ("hcp_population", "tests/data/drift/v1/population.csv", ["region", "annee"], "demo_v1"),
    ("hcp_population", "tests/data/drift/v2/population.csv", ["region", "annee"], "demo_v2"),
    ("hcp_population", "tests/data/drift/v3/population.csv", ["region", "annee"], "demo_v3"),
    ("hcp_population", "tests/data/drift/v2/population.csv", ["region", "annee"], "demo_bad"),
    ("world_bank_gdp", "tests/data/gold/world_bank_gdp_20240101.csv",
     ["country_code", "year"], "demo_gdp1"),
]  # fmt: skip


def build(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    stores = {n: LocalStore(root / n) for n in ("bronze", "silver", "gold", "quarantine")}
    engine = create_engine(f"sqlite:///{root / 'registry.db'}")
    metadata.create_all(engine)
    registry, bus = SchemaRegistry(engine), EventBus()
    policy = load_policy(REPO / "config/evolution_policy.yaml")
    dico = load_dictionary(REPO / "config/semantic/canonical.yaml")
    queue = ReviewQueue(engine, dico)
    mapper = LLMMapper(LocalProvider(dico), dico)
    for source, csv, pk, run_id in BATCHES:
        spec = SourceSpec(name=source, type="file", uri=csv, format="csv",  # type: ignore[arg-type]
                          frequency="manual", primary_key=pk)  # fmt: skip
        table = read_file(REPO / csv)
        if run_id == "demo_bad":  # synthetic corruption: out-of-range values -> quarantine
            i = table.columns.index("taux_chomage")
            for r in table.rows[:8]:
                r[i] = "250"
        bm = write_bronze(table, source=source, run_id=run_id, store=stores["bronze"])
        gate = run_schema_gate(
            profile_table(read_bronze_table(stores["bronze"], bm.parquet_path)),
            source_name=source, primary_key=pk, registry=registry, policy=policy, bus=bus,
            store=stores["bronze"], run_id=run_id,
        )  # fmt: skip
        print(f"{run_id}: schema v{gate.version} {gate.status} ({gate.overall_impact})")
        if gate.status == "blocked":
            continue
        res = run_silver_chain(
            table, spec=spec, schema_version=gate.version, dictionary=dico, mapper=mapper,
            queue=queue, store=stores["silver"], run_id=run_id, bronze_path=bm.parquet_path,
        )  # fmt: skip
        if res.status != "written" or res.manifest is None:
            print(f"{run_id}: silver {res.status} ({res.cause})")
            continue
        pr = promote_batch(
            source=source, silver_path=res.manifest.parquet_path, run_id=run_id,
            dictionary=dico, silver_store=stores["silver"], gold_store=stores["gold"],
            quarantine_store=stores["quarantine"], report_dir=root / "reports",
            engine=engine, schema_version=gate.version,
        )  # fmt: skip
        print(f"{run_id}: promotion {pr.status} score={pr.score}")
    built = build_all(stores["gold"], stores["gold"], load_products())
    print(f"gold products built: {[b.product for b in built]}")
    (root / MARKER).write_text("synthetic demo stores\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=Path("data/demo-store"))
    ap.add_argument(
        "--reset", action="store_true", help="rebuild (only a root made by this script)"
    )
    args = ap.parse_args(argv)
    root: Path = args.root.resolve()
    if root.exists() and (root / MARKER).exists():
        if args.reset:
            shutil.rmtree(root)
        else:
            print(f"{root} already built (idempotent); use --reset to rebuild")
    elif root.exists() and any(root.iterdir()):
        print(f"refusing: {root} is not empty and was not created by this script", file=sys.stderr)
        return 1
    if not (root / MARKER).exists():
        build(root)
    key = secrets.token_urlsafe(24)
    digest = "sha256:" + hashlib.sha256(key.encode()).hexdigest()
    print("\n# Demo API key (shown once, never stored). Start the API with:\n")
    print(f"export ADP_API_KEYS='{digest}'")  # the API only needs the digest
    for var, sub in (("ADP_GOLD_STORE_ROOT", "gold"), ("ADP_SILVER_STORE_ROOT", "silver"),
                     ("ADP_QUARANTINE_STORE_ROOT", "quarantine"),
                     ("ADP_REPORTS_DIR", "reports")):  # fmt: skip
        print(f"export {var}='{root / sub}'")
    print(f"export ADP_REGISTRY_URL='sqlite:///{root / 'registry.db'}'")
    print("uv run adp api serve --port 8000\n")
    print("# Then, for the Vite dev server (frontend/):\n")
    print(f"export ADP_API_KEY='{key}'")
    print("cd frontend && npm run dev")
    return 0


if __name__ == "__main__":
    sys.exit(main())
