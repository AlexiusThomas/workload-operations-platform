import { useEffect } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { useAuth } from "./auth/AuthContext";
import { useTutorial } from "./tutorial/TutorialContext";
import { NavBar } from "./components/NavBar";
import { ProtectedRoute } from "./components/ProtectedRoute";
import { Tutorial } from "./tutorial/Tutorial";
import { LoginPage } from "./pages/LoginPage";
import { QueuePage } from "./pages/QueuePage";
import { WorkUnitPage } from "./pages/WorkUnitPage";
import { MaterialsPage } from "./pages/MaterialsPage";
import { AuditPage } from "./pages/AuditPage";
import { ReportsPage } from "./pages/ReportsPage";
import { StalePage } from "./pages/StalePage";

export function App() {
  const { user } = useAuth();
  const { start, hasSeen } = useTutorial();

  // Show the tutorial on first use for a given synthetic user (where practical).
  useEffect(() => {
    if (user && !hasSeen(user.user_id)) {
      start();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user?.user_id]);

  return (
    <div className="app">
      <NavBar />
      <Tutorial />
      <Routes>
        <Route path="/" element={user ? <Navigate to="/queue" replace /> : <LoginPage />} />
        <Route path="/queue" element={<ProtectedRoute><QueuePage /></ProtectedRoute>} />
        <Route path="/work-units/:id" element={<ProtectedRoute><WorkUnitPage /></ProtectedRoute>} />
        <Route path="/materials" element={<ProtectedRoute><MaterialsPage /></ProtectedRoute>} />
        <Route path="/audit" element={<ProtectedRoute><AuditPage /></ProtectedRoute>} />
        <Route path="/reports" element={<ProtectedRoute><ReportsPage /></ProtectedRoute>} />
        <Route path="/stale" element={<ProtectedRoute><StalePage /></ProtectedRoute>} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </div>
  );
}
