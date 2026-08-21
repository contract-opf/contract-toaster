/**
 * dial-selector.test.tsx — the toaster's dial is the contract-type selector
 * (issue #490).
 *
 * The hero already HAD a dial: `review-playbook-dial` is an a11y-correct
 * radiogroup with arrow/Home/End support. But the type was picked from a pill
 * row under the illustration and the knob on the machine was decoration. The
 * ask was for the appliance itself to be the thing you set.
 *
 * ## What this file does and does not cover
 *
 * The radiogroup is deliberately untouched — same roles, same roving tabIndex,
 * same keyboard handling — and `dial-and-gallery` / `playbook-selector` still
 * own those assertions. This covers only the POINTER route that was added to
 * the same `onChange`, and the two properties that make it safe:
 *
 *   1. it commits on RELEASE, never during the drag
 *   2. it can only ever land on an ACTIVE stop
 *
 * (2) matters because an unactivated playbook fails closed at `load_playbook`,
 * so a dial that could park on one would be offering a guaranteed 503. The
 * arrow keys already skip them; the pointer has to as well, or the two routes
 * disagree about what is selectable.
 *
 * jsdom returns zeros from `getBoundingClientRect`, so the component's
 * client-to-user-space conversion is exercised with an explicitly stubbed
 * rect — a zero-size element has no meaningful angle and the code correctly
 * declines to invent one.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { ToasterHero, type DialEntry } from '../toaster/Toaster';

const THREE: DialEntry[] = [
  { playbook_id: 'nda', display_name: 'NDA', status: 'active' as const },
  { playbook_id: 'affiliation', display_name: 'Affiliation', status: 'active' as const },
  { playbook_id: 'msa', display_name: 'MSA', status: 'active' as const },
];

/** viewBox 420x340 rendered at 420x340 on screen: user units == client px. */
function stubRect() {
  vi.spyOn(SVGElement.prototype, 'getBoundingClientRect').mockReturnValue({
    x: 0,
    y: 0,
    left: 0,
    top: 0,
    right: 420,
    bottom: 340,
    width: 420,
    height: 340,
    toJSON: () => ({}),
  } as DOMRect);
}

const DIAL_CX = 146;
const DIAL_CY = 202;

/** A client point at `angle` (0 = north, clockwise) from the dial centre. */
function pointAt(angle: number, radius = 40) {
  const rad = (angle * Math.PI) / 180;
  return { clientX: DIAL_CX + radius * Math.sin(rad), clientY: DIAL_CY - radius * Math.cos(rad) };
}

function renderDial(entries: DialEntry[] = THREE, value = 'affiliation') {
  const onChange = vi.fn();
  render(
    <ToasterHero
      entries={entries}
      value={value}
      onChange={onChange}
      phase="idle"
      progressStage={null}
    />,
  );
  return { onChange, knob: screen.getByTestId('toaster-dial-knob') };
}

/** Grab the knob, drag to `angle`, release. */
function turnTo(knob: HTMLElement, angle: number) {
  fireEvent.pointerDown(knob, { pointerId: 1, ...pointAt(0) });
  fireEvent.pointerMove(knob, { pointerId: 1, ...pointAt(angle) });
  fireEvent.pointerUp(knob, { pointerId: 1, ...pointAt(angle) });
}

beforeEach(() => {
  vi.restoreAllMocks();
  stubRect();
});

describe('issue #490 — turning the dial selects the contract type', () => {
  it('a drag to the first stop selects it', () => {
    // Three stops spread across -60°..+60°, so stop 0 sits at -60°.
    const { onChange, knob } = renderDial();
    turnTo(knob, -60);
    expect(onChange).toHaveBeenCalledWith('nda');
  });

  it('a drag to the last stop selects it', () => {
    const { onChange, knob } = renderDial();
    turnTo(knob, 60);
    expect(onChange).toHaveBeenCalledWith('msa');
  });

  it('it SNAPS: an angle between two stops lands on the nearer one', () => {
    // 40° is between stop 1 (0°) and stop 2 (+60°), and nearer the latter.
    const { onChange, knob } = renderDial();
    turnTo(knob, 40);
    expect(onChange).toHaveBeenCalledWith('msa');
  });

  it('dragging past the last stop still commits that stop', () => {
    const { onChange, knob } = renderDial();
    turnTo(knob, 170);
    expect(onChange).toHaveBeenCalledWith('msa');
  });

  it('the NEEDLE stays inside the arc when the pointer leaves it', () => {
    // This is what the clamp is actually for, and I had it mislabelled: an
    // over-rotation commits the end stop with or without clamping, because
    // that stop is still the nearest. What the clamp prevents is the needle
    // itself swinging round the back of the dial while the finger is out
    // there — a pointer at 170° would otherwise draw the needle pointing
    // almost straight down, past a stop that does not exist.
    renderDial();
    const knob = screen.getByTestId('toaster-dial-knob');
    fireEvent.pointerDown(knob, { pointerId: 1, ...pointAt(0) });
    fireEvent.pointerMove(knob, { pointerId: 1, ...pointAt(170) });
    expect(
      Number(screen.getByTestId('toaster-dial-needle').getAttribute('data-angle')),
    ).toBe(60);
    fireEvent.pointerMove(knob, { pointerId: 1, ...pointAt(-170) });
    expect(
      Number(screen.getByTestId('toaster-dial-needle').getAttribute('data-angle')),
    ).toBe(-60);
  });

  it('it commits on RELEASE, not during the drag', () => {
    // A dial that fired onChange on every pointermove would change
    // submission-bound state at pointer rate and make "which type did I pick"
    // a race with the mouse.
    const { onChange, knob } = renderDial();
    fireEvent.pointerDown(knob, { pointerId: 1, ...pointAt(0) });
    fireEvent.pointerMove(knob, { pointerId: 1, ...pointAt(-60) });
    fireEvent.pointerMove(knob, { pointerId: 1, ...pointAt(60) });
    expect(onChange).not.toHaveBeenCalled();
    fireEvent.pointerUp(knob, { pointerId: 1, ...pointAt(60) });
    expect(onChange).toHaveBeenCalledTimes(1);
  });

  it('the needle tracks the finger mid-drag and reports its live angle', () => {
    renderDial();
    const knob = screen.getByTestId('toaster-dial-knob');
    fireEvent.pointerDown(knob, { pointerId: 1, ...pointAt(0) });
    fireEvent.pointerMove(knob, { pointerId: 1, ...pointAt(-45) });
    expect(
      Number(screen.getByTestId('toaster-dial-needle').getAttribute('data-angle')),
    ).toBeCloseTo(-45, 0);
  });

  it('a cancelled pointer selects nothing', () => {
    const { onChange, knob } = renderDial();
    fireEvent.pointerDown(knob, { pointerId: 1, ...pointAt(0) });
    fireEvent.pointerMove(knob, { pointerId: 1, ...pointAt(60) });
    fireEvent.pointerCancel(knob, { pointerId: 1 });
    expect(onChange).not.toHaveBeenCalled();
  });

  it('re-selecting the stop already chosen fires nothing', () => {
    const { onChange, knob } = renderDial(THREE, 'msa');
    turnTo(knob, 60);
    expect(onChange).not.toHaveBeenCalled();
  });
});

describe('issue #490 — it can only ever land on an active stop', () => {
  const WITH_COMING_SOON: DialEntry[] = [
    { playbook_id: 'nda', display_name: 'NDA', status: 'active' as const },
    { playbook_id: 'soon', display_name: 'MSA', status: 'coming_soon' as const },
    { playbook_id: 'affiliation', display_name: 'Affiliation', status: 'active' as const },
  ];

  it('a drag onto a coming-soon stop lands on the nearest active one instead', () => {
    // The coming-soon stop sits at 0°. Released at 10° the pointer is nearest
    // to it by a wide margin — 10° away, against 50° and 70° for the two
    // active stops — so a dial that did not filter would park there. An
    // unactivated playbook fails closed at load_playbook, so that would be
    // offering a guaranteed 503. The arrow keys already skip these; the
    // pointer has to agree or the two routes disagree about what is
    // selectable.
    //
    // Deliberately NOT released at exactly 0°: the two active stops are then
    // equidistant, which makes the outcome a tie-break rather than the
    // property under test.
    const onChange = vi.fn();
    render(
      <ToasterHero
        entries={WITH_COMING_SOON}
        value="nda"
        onChange={onChange}
        phase="idle"
        progressStage={null}
      />,
    );
    turnTo(screen.getByTestId('toaster-dial-knob'), 10);
    expect(onChange).toHaveBeenCalledWith('affiliation');
  });
});

describe('issue #490 — a single-stop deployment implies no dead affordance', () => {
  const ONE: DialEntry[] = [{ playbook_id: 'nda', display_name: 'NDA', status: 'active' }];

  it('the knob is not draggable and carries no grab cursor', () => {
    const { onChange, knob } = renderDial(ONE, 'nda');
    turnTo(knob, 60);
    expect(onChange).not.toHaveBeenCalled();
    expect(knob.getAttribute('class') ?? '').not.toContain('interactive');
  });

  it('two stops of which only one is active is also fixed', () => {
    // "How many stops are there" is not the question — "how many can you
    // actually turn to" is.
    const { onChange, knob } = renderDial(
      [
        { playbook_id: 'nda', display_name: 'NDA', status: 'active' as const },
        { playbook_id: 'soon', display_name: 'MSA', status: 'coming_soon' as const },
      ],
      'nda',
    );
    turnTo(knob, 60);
    expect(onChange).not.toHaveBeenCalled();
  });
});

describe('issue #490 — the radiogroup remains the accessible control', () => {
  it('the needle reflects the selected stop, so the two agree', () => {
    // The pill row below the hero is the labelled readout (a nameplate ON the
    // hero was built and removed — see the component comment: at 360px it
    // lands under 7 real pixels and overlaps the receipt slot). What the
    // appliance shows is the needle, and it has to point where the selection
    // says.
    renderDial(THREE, 'nda');
    expect(screen.getByTestId('toaster-dial-needle').getAttribute('data-angle')).toBe('-60');
  });

  it('the radiogroup is still the accessible control, unchanged', () => {
    renderDial();
    const group = screen.getByTestId('review-playbook-dial');
    expect(group).toHaveAttribute('role', 'radiogroup');
    // The knob is decoration-plus-pointer-affordance; it must not claim to be
    // a second radiogroup, or a screen reader is offered the same choice twice.
    expect(screen.getByTestId('toaster-dial-knob').getAttribute('role')).toBeNull();
  });
});
