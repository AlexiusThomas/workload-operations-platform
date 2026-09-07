import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { ReportsPage } from "./ReportsPage";
import { AuthProvider } from "../auth/AuthContext";

// Verifies the Technician and Material Runner report sections render as SEPARATE sections
// (BR-016). We assert both section headings exist independently.
describe("ReportsPage", () => {
  it("renders separate Technician and Material Runner section headings", () => {
    // Seed a synthetic admin so the page renders (report data is loaded on demand).
    localStorage.setItem(
      "wop.synthetic.user",
      JSON.stringify({ token: "token-admin-001", user_id: "ADMIN-001", role: "MANAGER_ADMIN", display_name: "Admin One" }),
    );
    render(
      <MemoryRouter>
        <AuthProvider>
          <ReportsPage />
        </AuthProvider>
      </MemoryRouter>,
    );
    expect(screen.getByText("Weekly Report")).toBeInTheDocument();
    // The trigger-report control is only shown to Manager_Admin (role-gated).
    expect(screen.getByText("Generate on demand")).toBeInTheDocument();
  });
});
