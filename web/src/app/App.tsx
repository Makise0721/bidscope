import { Routes, Route, Link } from "react-router-dom";
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
      </header>
      <main>
        <Routes>
          <Route path="/" element={<Workbench />} />
          <Route path="/runs/:runId" element={<RunReport />} />
        </Routes>
      </main>
    </div>
  );
}
