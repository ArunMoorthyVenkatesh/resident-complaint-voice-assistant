import { useState, useEffect, useCallback } from 'react';

/**
 * Guided tour, spotlighting one target element per step with a nearby
 * tooltip. Fully controlled: parent owns whether it's open (`open`) and
 * decides what happens when it's dismissed (`onClose`) -- lets it be shown
 * automatically on first visit AND reopened later via a "?" button.
 *
 * Steps whose target isn't currently in the DOM (e.g. the Insights section
 * only renders when there are alerts) are skipped automatically rather than
 * getting the tour stuck.
 */
export default function Walkthrough({ steps, open, onClose }) {
  const [stepIndex, setStepIndex] = useState(0);
  const [rect, setRect] = useState(null);

  useEffect(() => {
    if (open) setStepIndex(0);
  }, [open]);

  // Find (or skip) the target for the current step.
  useEffect(() => {
    if (!open) return;
    const el = document.querySelector(steps[stepIndex]?.selector);
    if (el) {
      setRect(el.getBoundingClientRect());
    } else if (stepIndex >= steps.length - 1) {
      onClose();
    } else {
      setStepIndex((i) => i + 1);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, stepIndex, steps]);

  // Keep the spotlight aligned with its target on resize/scroll.
  const reposition = useCallback(() => {
    if (!open) return;
    const el = document.querySelector(steps[stepIndex]?.selector);
    if (el) setRect(el.getBoundingClientRect());
  }, [open, stepIndex, steps]);

  useEffect(() => {
    window.addEventListener('resize', reposition);
    window.addEventListener('scroll', reposition, true);
    return () => {
      window.removeEventListener('resize', reposition);
      window.removeEventListener('scroll', reposition, true);
    };
  }, [reposition]);

  const next = () => {
    if (stepIndex >= steps.length - 1) onClose();
    else setStepIndex((i) => i + 1);
  };

  const prev = () => setStepIndex((i) => Math.max(0, i - 1));

  // Esc skips the tour; arrow keys step through it.
  useEffect(() => {
    if (!open) return;
    const handleKeyDown = (e) => {
      if (e.key === 'Escape') onClose();
      else if (e.key === 'ArrowRight' || e.key === 'ArrowDown') next();
      else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') prev();
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, onClose, stepIndex]);

  if (!open || !rect) return null;

  const pad = 8;
  const spotStyle = {
    top: rect.top - pad,
    left: rect.left - pad,
    width: rect.width + pad * 2,
    height: rect.height + pad * 2,
  };

  const spaceBelow = window.innerHeight - rect.bottom;
  const tooltipStyle = spaceBelow > 160
    ? { top: rect.bottom + pad + 10, left: Math.min(Math.max(rect.left, 16), window.innerWidth - 296) }
    : { top: Math.max(rect.top - pad - 150, 16), left: Math.min(Math.max(rect.left, 16), window.innerWidth - 296) };

  const step = steps[stepIndex];

  return (
    <div className="wt-overlay">
      <div className="wt-spotlight" style={spotStyle} />
      <div className="wt-tooltip" style={tooltipStyle}>
        <div className="wt-step-count">{stepIndex + 1} / {steps.length}</div>
        <div className="wt-title">{step.title}</div>
        <div className="wt-body">{step.body}</div>
        <div className="wt-actions">
          <button className="wt-skip" onClick={onClose}>Skip</button>
          <button className="wt-next" onClick={next}>
            {stepIndex >= steps.length - 1 ? 'Done' : 'Next'}
          </button>
        </div>
      </div>
    </div>
  );
}
