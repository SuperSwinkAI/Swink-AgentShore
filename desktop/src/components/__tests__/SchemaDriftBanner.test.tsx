import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

const { designateBeadsMigratorMock } = vi.hoisted(() => ({
  designateBeadsMigratorMock: vi.fn(),
}));

vi.mock("../../rpc/sessionClient", () => ({
  designateBeadsMigrator: designateBeadsMigratorMock,
}));

import { JsonRpcError } from "../../rpc/jsonrpc";
import { SchemaDriftBanner } from "../SchemaDriftBanner";
import type { SchemaDriftWarning } from "../../services/sidecarEvents";

const WARNING: SchemaDriftWarning = {
  sessionId: "session-1",
  projectPath: "/repo/project",
  remediation: "BD_ALLOW_REMOTE_MIGRATE=1 bd migrate && bd dolt push",
  error: "remote schema is 2 versions ahead of local",
};

describe("SchemaDriftBanner", () => {
  beforeEach(() => {
    designateBeadsMigratorMock.mockReset();
  });

  it("renders nothing when warning is null", () => {
    render(
      <SchemaDriftBanner warning={null} onDismiss={() => {}} onDesignated={() => {}} />,
    );
    expect(screen.queryByTestId("schema-drift-banner")).not.toBeInTheDocument();
  });

  it("renders the remediation command and error text when a warning is provided", () => {
    render(
      <SchemaDriftBanner warning={WARNING} onDismiss={() => {}} onDesignated={() => {}} />,
    );
    expect(screen.getByTestId("schema-drift-banner")).toHaveAttribute("role", "alert");
    expect(screen.getByTestId("schema-drift-error")).toHaveTextContent(WARNING.error);
    expect(screen.getByTestId("schema-drift-remediation")).toHaveTextContent(
      WARNING.remediation,
    );
  });

  it("calls designateBeadsMigrator and onDesignated on a successful click", async () => {
    designateBeadsMigratorMock.mockResolvedValueOnce({ designated: true });
    const onDesignated = vi.fn();
    render(
      <SchemaDriftBanner warning={WARNING} onDismiss={() => {}} onDesignated={onDesignated} />,
    );

    fireEvent.click(screen.getByTestId("schema-drift-designate"));

    expect(designateBeadsMigratorMock).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(onDesignated).toHaveBeenCalledTimes(1));
  });

  it("shows the error and does not call onDesignated when designateBeadsMigrator rejects", async () => {
    designateBeadsMigratorMock.mockRejectedValueOnce(
      new JsonRpcError({ code: -32000, message: "migration failed: network unreachable" }),
    );
    const onDesignated = vi.fn();
    render(
      <SchemaDriftBanner warning={WARNING} onDismiss={() => {}} onDesignated={onDesignated} />,
    );

    fireEvent.click(screen.getByTestId("schema-drift-designate"));

    expect(await screen.findByTestId("schema-drift-submit-error")).toHaveTextContent(
      "migration failed: network unreachable",
    );
    expect(onDesignated).not.toHaveBeenCalled();
    expect(screen.getByTestId("schema-drift-banner")).toBeInTheDocument();
  });

  it("calls onDismiss when the dismiss button is clicked", () => {
    const onDismiss = vi.fn();
    render(
      <SchemaDriftBanner warning={WARNING} onDismiss={onDismiss} onDesignated={() => {}} />,
    );

    fireEvent.click(screen.getByTestId("schema-drift-dismiss"));

    expect(onDismiss).toHaveBeenCalledTimes(1);
    expect(designateBeadsMigratorMock).not.toHaveBeenCalled();
  });
});
