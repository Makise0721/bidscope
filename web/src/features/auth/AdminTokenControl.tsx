import { FormEvent, useEffect, useState } from "react";
import { KeyRound, Save, Trash2 } from "lucide-react";
import {
  clearAdminToken,
  getAdminToken,
  onUnauthorized,
  setAdminToken,
} from "../../auth/adminToken";

interface AdminTokenControlProps {
  onTokenChanged?: () => void;
}

export function AdminTokenControl({ onTokenChanged }: AdminTokenControlProps) {
  const [value, setValue] = useState("");
  const [configured, setConfigured] = useState(() => Boolean(getAdminToken()));
  const [validationError, setValidationError] = useState(false);

  useEffect(() => {
    return onUnauthorized(() => {
      setConfigured(false);
      setValue("");
    });
  }, []);

  const handleSave = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const saved = setAdminToken(value);
    if (!saved) {
      setValidationError(true);
      return;
    }
    setValue("");
    setValidationError(false);
    setConfigured(true);
    onTokenChanged?.();
  };

  const handleClear = () => {
    clearAdminToken();
    setValue("");
    setValidationError(false);
    setConfigured(false);
    onTokenChanged?.();
  };

  return (
    <form className="admin-token-control" onSubmit={handleSave} aria-label="Admin token access">
      <KeyRound size={16} aria-hidden="true" />
      <label htmlFor="admin-token-input">Admin token</label>
      <input
        id="admin-token-input"
        type="password"
        value={value}
        autoComplete="off"
        placeholder={configured ? "Token saved for this tab" : "Enter token"}
        onChange={(event) => {
          setValue(event.target.value);
          setValidationError(false);
        }}
        aria-describedby="admin-token-status"
      />
      <button type="submit" className="secondary-button" disabled={!value.trim()}>
        <Save size={15} aria-hidden="true" />
        Save
      </button>
      <button
        type="button"
        className="secondary-button"
        onClick={handleClear}
        disabled={!configured && !value}
        aria-label="Clear admin token"
      >
        <Trash2 size={15} aria-hidden="true" />
        Clear
      </button>
      <span id="admin-token-status" className="admin-token-status" role="status">
        {validationError
          ? "Enter an Admin Token."
          : configured
            ? "Token saved for this tab."
            : "Token required for API access."}
      </span>
    </form>
  );
}
