import { Link, NavLink, Outlet } from "react-router-dom";
import { useConfig } from "../lib/hooks";

export default function Layout() {
  const config = useConfig();
  return (
    <>
      <header className="topbar">
        <div className="topbar-inner">
          <Link to="/" className="wordmark">
            Vouch
          </Link>
          <nav className="nav">
            <NavLink to="/" end>
              Applications
            </NavLink>
            <NavLink to="/phone">Phone</NavLink>
            <NavLink to="/settings">Settings</NavLink>
          </nav>
          <div className="topbar-right">{config ? <span>{config.seller_name} credit desk</span> : null}</div>
        </div>
      </header>
      <Outlet />
    </>
  );
}
