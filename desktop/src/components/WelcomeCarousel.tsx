import { useEffect, useState, type JSX } from "react";
import styles from "./WelcomeCarousel.module.css";

type Slide = {
  /** Decorative glyph — one emoji per slide, no sprites (per the design spec). */
  icon: string;
  title: string;
  body: JSX.Element;
};

const SLIDES: Slide[] = [
  {
    icon: "🧭",
    title: "Welcome to AgentShore",
    body: (
      <>
        <p>
          AgentShore is a reinforcement-learning <strong>orchestrator</strong>. It
          coordinates CLI coding agents — Claude Code, Codex, Grok, and
          Antigravity — to work through your backlog.
        </p>
        <p>
          It decides <em>what to do next and who does it</em>. It does not write the
          code itself — the agents do.
        </p>
      </>
    ),
  },
  {
    icon: "🔁",
    title: "How a session works",
    body: (
      <>
        <p>You pick a repository and a budget. From there the loop runs itself:</p>
        <ul>
          <li>Agents pick up issues and open pull requests.</li>
          <li>Other agents review each other&apos;s PRs.</li>
          <li>You watch it happen live on the dashboard.</li>
        </ul>
      </>
    ),
  },
  {
    icon: "🧰",
    title: "What you'll need",
    body: (
      <>
        <ul>
          <li>A git repository to work in.</li>
          <li>
            <strong>At least 2 supported agent CLIs</strong> installed.
          </li>
          <li>
            <strong>At least 2 GitHub accounts.</strong> A reviewer can never
            approve their own PR, so review only happens with a second agent and a
            second identity.
          </li>
        </ul>
        <p className={styles.recommended}>
          <strong>Recommended:</strong> give each agent harness its own GitHub
          identity — one per harness — for fully auditable attribution.
        </p>
      </>
    ),
  },
  {
    icon: "🚀",
    title: "Ready to go",
    body: (
      <>
        <p>
          That&apos;s the whole idea. Choose a project to begin, or open a folder to
          set one up.
        </p>
        <p>
          You can replay this tour anytime from <strong>Help ▸ Welcome Tour</strong>.
        </p>
      </>
    ),
  },
];

const LAST = SLIDES.length - 1;

/**
 * First-run welcome carousel. There is a single source of truth — the parent's
 * persisted `onboarding_completed` flag, surfaced here as `seen`:
 *  - `onSeen()` fires when the user reaches the last slide (the spec's "reaching
 *    the last slide marks seen"). The "Don't show again" checkbox is just a live
 *    view of that same flag, so once seen it reads as checked.
 *  - `onClose()` only hides the carousel. Closing early (X / Esc before the last
 *    slide, while `seen` is still false) leaves the flag untouched, so it
 *    re-shows next launch.
 */
export function WelcomeCarousel({
  open,
  seen,
  onSeen,
  onSeenChange,
  onClose,
}: {
  open: boolean;
  /** Mirror of the persisted `onboarding_completed` flag; drives the checkbox. */
  seen: boolean;
  /** Mark seen (idempotent) — called when the last slide is reached. */
  onSeen: () => void;
  /** Checkbox toggled: set the persisted flag to `next`. */
  onSeenChange: (next: boolean) => void;
  /** Hide the carousel (X / Esc / Get started). */
  onClose: () => void;
}): JSX.Element | null {
  const [slide, setSlide] = useState(0);

  // Restart at slide 0 each time the carousel is (re)opened, including replay
  // from the Help menu.
  useEffect(() => {
    if (open) {
      setSlide(0);
    }
  }, [open]);

  // Reaching the final slide marks the flow seen (covers Next, dots, and arrow
  // navigation, since they all funnel through `slide`). Idempotent on the parent.
  useEffect(() => {
    if (open && slide === LAST) {
      onSeen();
    }
  }, [open, slide, onSeen]);

  // Keyboard nav while open: Esc hides, arrows page. Registered on window so it
  // works regardless of focus; torn down when closed/unmounted.
  useEffect(() => {
    if (!open) {
      return undefined;
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
      } else if (event.key === "ArrowRight") {
        event.preventDefault();
        setSlide((s) => Math.min(s + 1, LAST));
      } else if (event.key === "ArrowLeft") {
        event.preventDefault();
        setSlide((s) => Math.max(s - 1, 0));
      }
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
    };
  }, [open, onClose]);

  if (!open) {
    return null;
  }

  const current = SLIDES[slide] ?? SLIDES[0];
  const onLast = slide >= LAST;

  return (
    // Backdrop intentionally has no onClick — a stray click must not dismiss
    // the first-run flow (mirrors the shared Modal's default).
    <div
      className={styles.overlay}
      role="dialog"
      aria-modal="true"
      aria-label="Welcome to AgentShore"
      data-testid="welcome-carousel"
    >
      <div className={styles.dialog}>
        <button
          type="button"
          className={styles.close}
          onClick={onClose}
          aria-label="Close"
          data-testid="welcome-carousel-close"
        >
          ×
        </button>

        <div className={styles.slide}>
          <div className={styles.icon} aria-hidden="true">
            {current.icon}
          </div>
          <h2 className={styles.title}>{current.title}</h2>
          <div className={styles.body}>{current.body}</div>
        </div>

        <div className={styles.dots} role="tablist" aria-label="Slides">
          {SLIDES.map((s, i) => (
            <button
              key={s.title}
              type="button"
              role="tab"
              aria-selected={i === slide}
              aria-label={`Go to slide ${i + 1}: ${s.title}`}
              className={`${styles.dot} ${i === slide ? styles.dotActive : ""}`}
              onClick={() => setSlide(i)}
              data-testid={`welcome-carousel-dot-${i}`}
            />
          ))}
        </div>

        <div className={styles.footer}>
          <label className={styles.dontShow}>
            <input
              type="checkbox"
              checked={seen}
              onChange={(event) => onSeenChange(event.target.checked)}
              data-testid="welcome-carousel-dont-show"
            />
            Don&apos;t show again
          </label>

          <div className={styles.actions}>
            {slide > 0 && (
              <button
                type="button"
                className={styles.button}
                onClick={() => setSlide((s) => Math.max(s - 1, 0))}
                data-testid="welcome-carousel-back"
              >
                Back
              </button>
            )}
            {onLast ? (
              <button
                type="button"
                className={`${styles.button} ${styles.buttonPrimary}`}
                onClick={onClose}
                data-testid="welcome-carousel-cta"
              >
                Get started
              </button>
            ) : (
              <button
                type="button"
                className={`${styles.button} ${styles.buttonPrimary}`}
                onClick={() => setSlide((s) => Math.min(s + 1, LAST))}
                data-testid="welcome-carousel-next"
              >
                Next
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
