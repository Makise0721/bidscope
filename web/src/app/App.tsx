import { useEffect, useState } from "react";
import { Routes, Route, Link } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { EvaluationView } from "../features/evaluation/EvaluationView";
import { RunHistory } from "../features/runs/RunHistory";
import { SourcesView } from "../features/sources/SourcesView";
import { SubscriptionsView } from "../features/subscriptions/SubscriptionsView";
import { Workbench } from "../features/workbench/Workbench";
import { RunReportRoute } from "../features/workbench/RunReport";
import { AdminTokenControl } from "../features/auth/AdminTokenControl";
import {
  onAdminTokenChange,
} from "../auth/adminToken";
import "../styles/workbench.css";

export function App() {
  const queryClient = useQueryClient();
  const [, setAuthRevision] = useState(0);

  useEffect(() => {
    const refreshQueries = () => {
      setAuthRevision((revision) => revision + 1);
      void queryClient.invalidateQueries();
    };
    const unsubscribeToken = onAdminTokenChange(refreshQueries);
    return () => {
      unsubscribeToken();
    };
  }, [queryClient]);

  return (
    <div className="app">
      <header className="app-header">
        <div className="app-header-main">
          <Link to="/" className="app-title">
            BidScope
          </Link>
          <nav className="app-nav" aria-label="Operations">
            <Link to="/runs">Runs</Link>
            <Link to="/subscriptions">Subscriptions</Link>
            <Link to="/sources">Sources</Link>
            <Link to="/evaluation">Evaluation</Link>
          </nav>
        </div>
        <AdminTokenControl />
      </header>
      <main>
        <Routes>
          <Route path="/" element={<Workbench />} />
          <Route path="/runs" element={<RunHistory />} />
          <Route path="/runs/:runId" element={<RunReportRoute />} />
          <Route path="/subscriptions" element={<SubscriptionsView />} />
          <Route path="/sources" element={<SourcesView />} />
          <Route path="/evaluation" element={<EvaluationView />} />
        </Routes>
      </main>
    </div>
  );
}
