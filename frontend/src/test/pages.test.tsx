import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import * as fx from "./fixtures";
import { mockApi, renderAt, status } from "./helpers";

const api = () =>
  mockApi({
    "/runs": () => fx.page(fx.runs),
    "/quality/scores": (u) => fx.page(fx.scores.filter((s) => s.source_name === (u.searchParams.get("source") ?? s.source_name))),
    "/quality/reports/*": () => fx.report,
    "/quality/quarantine/*": (u) => {
      const off = Number(u.searchParams.get("offset") ?? 0);
      return fx.page(fx.quarantineRows(Math.min(10, 12 - off), off), 12, 10, off);
    },
    "/schemas/hcp_population/diff": () => fx.page(fx.diff),
    "/schemas/*": (u) => (u.pathname.endsWith("hcp_population") ? fx.page(fx.versions) : status(404)),
  });

describe("Pipeline", () => {
  it("shows a loading state then the latest run of each source", async () => {
    api();
    renderAt("/");
    expect(screen.getByRole("status")).toHaveTextContent("Chargement");
    const table = await screen.findByRole("table", { name: /Dernier run de chaque source/ });
    const rows = within(table).getAllByRole("row");
    expect(rows).toHaveLength(3); // header + 2 sources
    expect(within(rows[1]!).getByText("hcp_population")).toBeInTheDocument();
    expect(within(rows[1]!).getByText("demo_bad")).toBeInTheDocument();
    expect(within(rows[1]!).getByText("8 lignes isolées")).toBeInTheDocument();
    expect(within(rows[1]!).getByText("Quarantaine partielle")).toBeInTheDocument();
    expect(within(rows[1]!).getByRole("img", { name: "Score 81,2 sur 100" })).toBeInTheDocument();
    expect(await within(rows[1]!).findByText("v3 à revoir")).toBeInTheDocument();
    expect(within(rows[2]!).getByText("Promu")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Pipeline", level: 1 })).toBeInTheDocument();
  });

  it("shows an explicit empty state", async () => {
    mockApi({ "/runs": () => fx.page([]) });
    renderAt("/");
    expect(await screen.findByText(/Aucun run enregistré/)).toBeInTheDocument();
  });

  it("shows an alert with retry on error, and the 401 message", async () => {
    let fail = true;
    mockApi({ "/runs": () => (fail ? status(401) : fx.page(fx.runs)), "/schemas/*": () => fx.page(fx.versions) });
    renderAt("/");
    expect(await screen.findByRole("alert")).toHaveTextContent("Clé d'API invalide ou absente");
    fail = false;
    await userEvent.click(screen.getByRole("button", { name: "Réessayer" }));
    expect(await screen.findByRole("table")).toBeInTheDocument();
  });
});

describe("Quality", () => {
  it("renders KPIs, columns, failed rules and paginates the quarantine", async () => {
    const { calls } = api();
    renderAt("/qualite");
    expect(await screen.findByText("Score global (hcp_population)")).toBeInTheDocument();
    expect(await screen.findByRole("img", { name: "Score 62,0 sur 100" })).toBeInTheDocument();
    expect(screen.getAllByText("unemployment_pct").length).toBeGreaterThan(0);
    expect(screen.getByText("Bloquante")).toBeInTheDocument();
    const q = await screen.findByRole("table", { name: /Lignes isolées de hcp_population/ });
    expect(within(q).getAllByRole("row")).toHaveLength(11);
    expect(screen.getByText("1 à 10 sur 12")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Précédent" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Suivant" }));
    expect(await screen.findByText("11 à 12 sur 12")).toBeInTheDocument();
    expect(calls.some((u) => u.pathname.includes("quarantine") && u.searchParams.get("offset") === "10")).toBe(true);
    expect(screen.getByRole("button", { name: "Suivant" })).toBeDisabled();
  });

  it("shows the empty quarantine and a missing report as honest states", async () => {
    mockApi({
      "/runs": () => fx.page(fx.runs),
      "/quality/scores": () => fx.page(fx.scores),
      "/quality/reports/*": () => status(404),
      "/quality/quarantine/*": () => fx.page([]),
    });
    renderAt("/qualite");
    expect(await screen.findByText(/Aucune ligne en quarantaine/)).toBeInTheDocument();
    expect((await screen.findAllByRole("alert"))[0]).toHaveTextContent("introuvable");
  });
});

describe("Schemas", () => {
  it("lists versions and the diff sorted by gravity with impact badges", async () => {
    api();
    renderAt("/schemas");
    expect(await screen.findByRole("heading", { name: "Différences v2 vers v3" })).toBeInTheDocument();
    expect(screen.getByText("En attente de revue")).toBeInTheDocument();
    const list = await screen.findByRole("list", { name: "Changements" });
    const items = within(list).getAllByRole("listitem");
    expect(items).toHaveLength(3);
    expect(items[0]).toHaveTextContent("drop_column : taux_activite");
    expect(items[0]).toHaveTextContent("Bloquant");
    expect(items[1]).toHaveTextContent("À revoir");
    expect(items[2]).toHaveTextContent("Compatible");
    expect(screen.getByText("Impact global :")).toHaveTextContent("Bloquant");
  });

  it("changes the compared versions from the keyboard", async () => {
    const { calls } = api();
    renderAt("/schemas");
    await screen.findByRole("list", { name: "Changements" });
    await userEvent.selectOptions(screen.getByLabelText("De"), "1");
    await waitFor(() => expect(calls.some((u) => u.pathname.endsWith("/diff") && u.searchParams.get("from") === "1")).toBe(true));
  });
});

describe("Review (consultative, H-33)", () => {
  it("shows an explicit unavailable state with the CLI, and calls no API", async () => {
    const { fn } = mockApi({});
    renderAt("/revue");
    expect(screen.getByRole("heading", { name: "Revue du mapping", level: 1 })).toBeInTheDocument();
    expect(screen.getByText("Indisponible via l'API")).toBeInTheDocument();
    expect(screen.getByText("adp review list --status pending")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Approuver|Rejeter/ })).toBeNull();
    expect(fn).not.toHaveBeenCalled();
  });
});
