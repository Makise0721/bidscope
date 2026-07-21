import { Routes, Route, Link } from "react-router-dom";
import { EvaluationView } from "../features/evaluation/EvaluationView";
import { RunHistory } from "../features/runs/RunHistory";
import { SourcesView } from "../features/sources/SourcesView";
import { SubscriptionsView } from "../features/subscriptions/SubscriptionsView";
import { Workbench } from "../features/workbench/Workbench";
import { RunReport } from "../features/workbench/RunReport";
import "../styles/workbench.css";

export function App() {
  return (
    <div className="app">
      <header className="app-header">
        <Link to="/" className="app-title">
          BidScope
        </Link>
        <nav className="app-nav" aria-label="Operations">
          <Link to="/runs">Runs</Link>
          <Link to="/subscriptions">Subscriptions</Link>
          <Link to="/sources">Sources</Link>
          <Link to="/evaluation">Evaluation</Link>
        </nav>
      </header>
      <main>
        <Routes>
          <Route path="/" element={<Workbench />} />
          <Route path="/runs" element={<RunHistory />} />
          <Route path="/runs/:runId" element={<RunReport />} />
          <Route path="/subscriptions" element={<SubscriptionsView />} />
          <Route path="/sources" element={<SourcesView />} />
          <Route path="/evaluation" element={<EvaluationView />} />
        </Routes>
      </main>
    </div>
  );
}
