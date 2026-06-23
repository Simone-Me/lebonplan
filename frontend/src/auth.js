import { clearAccessToken, fetchCurrentUser, getAccessToken, login, setAccessToken } from "./api.js";

function setOverlayVisible(visible) {
  const overlay = document.getElementById("auth-overlay");
  overlay.classList.toggle("hidden", !visible);
  if (visible) {
    document.getElementById("auth-username")?.focus();
  }
}

function setAuthError(message = "") {
  const error = document.getElementById("auth-error");
  error.textContent = message;
  error.classList.toggle("hidden", !message);
}

function setUsernameLabel(username) {
  const label = document.getElementById("auth-user");
  const logoutBtn = document.getElementById("auth-logout");
  const connected = Boolean(username);
  if (label) {
    label.textContent = connected ? `Connecte : ${username}` : "";
    label.classList.toggle("hidden", !connected);
  }
  if (logoutBtn) {
    logoutBtn.classList.toggle("hidden", !connected);
  }
}

export async function initAuth(onAuthenticated) {
  const form = document.getElementById("auth-form");
  const logoutBtn = document.getElementById("auth-logout");
  const usernameInput = document.getElementById("auth-username");
  const passwordInput = document.getElementById("auth-password");

  async function validateExistingToken() {
    const token = getAccessToken();
    if (!token) {
      setOverlayVisible(true);
      setUsernameLabel("");
      return false;
    }

    try {
      const user = await fetchCurrentUser();
      setOverlayVisible(false);
      setUsernameLabel(user.username);
      await onAuthenticated?.();
      return true;
    } catch (_) {
      clearAccessToken();
      setOverlayVisible(true);
      setUsernameLabel("");
      return false;
    }
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    setAuthError("");
    try {
      const result = await login(usernameInput.value.trim(), passwordInput.value);
      setAccessToken(result.access_token);
      const user = await fetchCurrentUser();
      setOverlayVisible(false);
      setUsernameLabel(user.username);
      passwordInput.value = "";
      await onAuthenticated?.();
    } catch (_) {
      setAuthError("Connexion refusée. Vérifiez le nom d'utilisateur et le mot de passe.");
    }
  });

  logoutBtn.addEventListener("click", () => {
    clearAccessToken();
    setOverlayVisible(true);
    setUsernameLabel("");
    setAuthError("");
  });

  window.addEventListener("auth-required", () => {
    setOverlayVisible(true);
    setUsernameLabel("");
    setAuthError("Session expirée. Reconnectez-vous.");
  });

  await validateExistingToken();
}
