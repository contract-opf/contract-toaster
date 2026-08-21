/**
 * ErrorBoundary — one panel dying must not take the app with it (issue #487).
 *
 * There was no boundary anywhere in this tree, so ANY render-time exception in
 * ANY panel — a malformed API payload a type guard missed, a null the code did
 * not expect — white-screened all eight tabs at once, including a review in
 * flight. For a legal tool the failure mode has to be "this screen hit a
 * problem", never a dead page.
 *
 * ## Where it goes, and why there
 *
 * At the panel seam. The tab panels in App.tsx are already mounted
 * independently and toggled with `hidden` (so a running review keeps polling
 * behind a hidden tab), which means the seam this needs already exists — a
 * boundary per panel isolates exactly the unit a user thinks of as "a screen".
 * A single top-level boundary would catch the same throw and still blank
 * everything, which is the behaviour being fixed.
 *
 * A top-level one is mounted too, as the last resort for a throw outside any
 * panel (the header, the tab bar). It is not the primary defence.
 *
 * ## What reaches the DOM
 *
 * Friendly copy only. The `componentStack` and the error go to the console,
 * the same posture `friendlyErrorMessage` already takes for network failures:
 * a legal tool must not paint a stack trace, or an exception message that may
 * quote document substance, onto the page.
 *
 * ## Recovery
 *
 * "Reload this screen" clears the flag, and that is genuinely enough: React
 * UNMOUNTS the whole subtree below a boundary when it catches, so the
 * children mount fresh rather than resuming from the state that broke them.
 *
 * (A key bump was in here first, to force that remount. Mutation testing
 * showed removing it changed nothing — React had already done the unmounting —
 * so it went, rather than stay as a line of code whose comment claimed a
 * guarantee it was not providing.)
 *
 * The button matters because React never resets a boundary on its own: without
 * it the fallback is permanent until a full page reload, and on a
 * password-mode deployment a reload used to sign the user out (#468).
 */
import { Component, type ErrorInfo, type ReactNode } from 'react';
import { CtButton, CtCard } from './ui/react';

export const PANEL_ERROR_COPY =
  'This screen hit a problem. The rest of the app is unaffected — switch tabs, or reload this screen.';

interface ErrorBoundaryProps {
  children: ReactNode;
  /** Distinguishes one boundary's fallback from another's in tests. */
  name: string;
}

interface ErrorBoundaryState {
  hasError: boolean;
}

export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { hasError: false };

  static getDerivedStateFromError(): ErrorBoundaryState {
    return { hasError: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Console only. Never the DOM — see the module docstring.
    // eslint-disable-next-line no-console
    console.error(`[${this.props.name}] panel error`, error, info.componentStack);
  }

  private retry = (): void => {
    this.setState({ hasError: false });
  };

  render(): ReactNode {
    if (!this.state.hasError) {
      return <>{this.props.children}</>;
    }
    return (
      <CtCard data-testid={`panel-error-${this.props.name}`}>
        <div className="ct-stack">
          <p>{PANEL_ERROR_COPY}</p>
          <div className="ct-actions" role="group">
            <CtButton
              type="button"
              variant="secondary"
              size="sm"
              data-testid={`panel-error-retry-${this.props.name}`}
              onClick={this.retry}
            >
              Reload this screen
            </CtButton>
          </div>
        </div>
      </CtCard>
    );
  }
}
