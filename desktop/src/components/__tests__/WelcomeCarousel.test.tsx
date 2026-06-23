import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

import { WelcomeCarousel } from "../WelcomeCarousel";

function renderCarousel(overrides: Partial<{ open: boolean; seen: boolean }> = {}) {
  const handlers = {
    onSeen: vi.fn(() => {}),
    onSeenChange: vi.fn((_next: boolean) => {}),
    onClose: vi.fn(() => {}),
  };
  render(
    <WelcomeCarousel
      open={overrides.open ?? true}
      seen={overrides.seen ?? false}
      onSeen={handlers.onSeen}
      onSeenChange={handlers.onSeenChange}
      onClose={handlers.onClose}
    />,
  );
  return handlers;
}

// Walk to the final slide via the Next button.
function advanceToLastSlide() {
  // 4 slides → 3 Next clicks.
  fireEvent.click(screen.getByTestId("welcome-carousel-next"));
  fireEvent.click(screen.getByTestId("welcome-carousel-next"));
  fireEvent.click(screen.getByTestId("welcome-carousel-next"));
}

describe("WelcomeCarousel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders nothing when closed", () => {
    renderCarousel({ open: false });
    expect(screen.queryByTestId("welcome-carousel")).not.toBeInTheDocument();
  });

  it("opens on the first slide with Back hidden", () => {
    renderCarousel();
    expect(screen.getByTestId("welcome-carousel")).toBeInTheDocument();
    expect(screen.getByText("Welcome to AgentShore")).toBeInTheDocument();
    expect(screen.queryByTestId("welcome-carousel-back")).not.toBeInTheDocument();
    expect(screen.getByTestId("welcome-carousel-next")).toBeInTheDocument();
  });

  it("navigates forward and back with the buttons", () => {
    renderCarousel();
    fireEvent.click(screen.getByTestId("welcome-carousel-next"));
    expect(screen.getByText("How a session works")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("welcome-carousel-back"));
    expect(screen.getByText("Welcome to AgentShore")).toBeInTheDocument();
  });

  it("jumps to a slide via the progress dots", () => {
    renderCarousel();
    fireEvent.click(screen.getByTestId("welcome-carousel-dot-2"));
    expect(screen.getByText("What you'll need")).toBeInTheDocument();
  });

  it("navigates with the arrow keys", () => {
    renderCarousel();
    fireEvent.keyDown(window, { key: "ArrowRight" });
    expect(screen.getByText("How a session works")).toBeInTheDocument();
    fireEvent.keyDown(window, { key: "ArrowLeft" });
    expect(screen.getByText("Welcome to AgentShore")).toBeInTheDocument();
  });

  it("does not mark seen or close on an early X click", () => {
    const h = renderCarousel();
    fireEvent.click(screen.getByTestId("welcome-carousel-close"));
    expect(h.onClose).toHaveBeenCalledTimes(1);
    expect(h.onSeen).not.toHaveBeenCalled();
  });

  it("closes early on Esc without marking seen", () => {
    const h = renderCarousel();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(h.onClose).toHaveBeenCalledTimes(1);
    expect(h.onSeen).not.toHaveBeenCalled();
  });

  it("marks seen when the last slide is reached and closes via Get started", () => {
    const h = renderCarousel();
    advanceToLastSlide();
    // Reaching the final slide marks the flow seen.
    expect(h.onSeen).toHaveBeenCalled();
    const cta = screen.getByTestId("welcome-carousel-cta");
    expect(cta).toHaveTextContent("Get started");
    fireEvent.click(cta);
    expect(h.onClose).toHaveBeenCalledTimes(1);
  });

  it("marks seen when jumping straight to the last slide via a dot", () => {
    const h = renderCarousel();
    fireEvent.click(screen.getByTestId("welcome-carousel-dot-3"));
    expect(h.onSeen).toHaveBeenCalled();
  });

  it("reflects the persisted flag in the checkbox and reports toggles", () => {
    const h = renderCarousel({ seen: true });
    const checkbox = screen.getByTestId("welcome-carousel-dont-show");
    expect(checkbox).toBeChecked();
    fireEvent.click(checkbox);
    expect(h.onSeenChange).toHaveBeenCalledWith(false);
  });

  it("does not close when the backdrop is clicked", () => {
    const h = renderCarousel();
    fireEvent.click(screen.getByTestId("welcome-carousel"));
    expect(h.onClose).not.toHaveBeenCalled();
  });
});
