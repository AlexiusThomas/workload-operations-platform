import { Link } from "react-router-dom";
import { api } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { useAsync } from "../hooks/useAsync";
import { EmptyState, ErrorState, Loading } from "../components/StateViews";

export function StalePage() {
  const { user } = useAuth();
  const token = user!.token;
  const { data, loading, error, reload } = useAsync(() => api.listStale(token), []);

  return (
    <main className="page stale-page">
      <h1>Stale Active Work</h1>
      {loading && <Loading />}
      {error != null && <ErrorState error={error} onRetry={reload} />}
      {!loading && !error && data && data.items.length === 0 && (
        <EmptyState message="No stale active work." />
      )}
      {!loading && !error && data && data.items.length > 0 && (
        <ul>
          {data.items.map((wu) => (
            <li key={wu.work_unit_id}>
              <Link to={`/work-units/${wu.work_unit_id}`}>{wu.work_unit_id}</Link> — {wu.state} ({wu.current_scheduled_date})
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}
