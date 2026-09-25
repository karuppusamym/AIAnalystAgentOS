import { useId } from "react";
import { AUTONOMY_LEVELS } from "../lib/status";

export function AutonomyPicker({ value, onChange, legend = "Autonomy level" }: { value: number; onChange: (v: number) => void; legend?: string }) {
  const name = useId();
  return (
    <fieldset className="autonomy">
      <legend>{legend}</legend>
      {AUTONOMY_LEVELS.map((l) => (
        <label key={l.level} className={`autonomy-option ${value === l.level ? "selected" : ""}`}>
          <input type="radio" name={name} value={l.level} checked={value === l.level} onChange={() => onChange(l.level)} />
          <span className="autonomy-level">L{l.level}</span>
          <span>
            <strong>{l.name}</strong>
            <span className="muted small block">{l.description}</span>
          </span>
        </label>
      ))}
      <p className="field-hint">Publication always requires a human approval in this release, whatever the level.</p>
    </fieldset>
  );
}
