import { NavLink, Outlet } from "react-router-dom";
import { useTheme } from "../theme";

const LINKS = [
  { to: "/", label: "Pipeline", end: true },
  { to: "/qualite", label: "Qualité" },
  { to: "/schemas", label: "Schémas" },
  { to: "/revue", label: "Revue du mapping" },
];

export function Layout() {
  const { theme, toggle } = useTheme();
  return (
    <>
      <a className="skip" href="#contenu">
        Aller au contenu
      </a>
      <div className="app">
        <aside className="side">
          <span className="brand">
            ADP <span>Plateforme de données</span>
          </span>
          <nav aria-label="Navigation principale">
            <ul className="nav">
              {LINKS.map((l) => (
                <li key={l.to}>
                  <NavLink to={l.to} end={l.end}>
                    {l.label}
                  </NavLink>
                </li>
              ))}
            </ul>
          </nav>
        </aside>
        <main id="contenu" tabIndex={-1}>
          <div className="flex justify-end">
            <button className="btn" type="button" aria-pressed={theme === "dark"} onClick={toggle}>
              Thème clair / sombre
            </button>
          </div>
          <Outlet />
        </main>
      </div>
    </>
  );
}
