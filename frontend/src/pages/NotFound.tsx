import { Link } from "react-router-dom";

export default function NotFound() {
  return (
    <main className="page">
      <h1>Page not found</h1>
      <p className="muted" style={{ marginTop: 12 }}>
        <Link to="/">Back to applications</Link>
      </p>
    </main>
  );
}
