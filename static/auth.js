// Supabase Auth wrapper. supabase-js is used for authentication only; all data goes through our API.
let clientPromise;

async function loadConfig() {
  const response = await fetch("/config.json", { cache: "no-cache" });
  if (!response.ok) throw new Error("Could not load application configuration.");
  return response.json();
}

export function getConfig() {
  clientPromise ??= loadConfig();
  return clientPromise;
}

let supabasePromise;
export function getClient() {
  supabasePromise ??= getConfig().then((config) => {
    if (!window.supabase?.createClient) throw new Error("Authentication library failed to load.");
    return window.supabase.createClient(config.supabase_url, config.supabase_anon_key, {
      auth: { persistSession: true, autoRefreshToken: true, detectSessionInUrl: true, flowType: "pkce" },
    });
  });
  return supabasePromise;
}

export async function getSession() {
  const client = await getClient();
  const { data } = await client.auth.getSession();
  return data.session;
}

export async function signIn(email, password) {
  const client = await getClient();
  const { error } = await client.auth.signInWithPassword({ email, password });
  if (error) throw error;
}

export async function signUp(name, email, password) {
  const client = await getClient();
  const { data, error } = await client.auth.signUp({
    email, password,
    options: { data: { full_name: name }, emailRedirectTo: `${location.origin}/login` },
  });
  if (error) throw error;
  // With email confirmation on, there is no session yet.
  return { needsConfirmation: !data.session };
}

export async function signInWithGoogle() {
  const client = await getClient();
  const { error } = await client.auth.signInWithOAuth({
    provider: "google", options: { redirectTo: `${location.origin}/app` },
  });
  if (error) throw error;
}

export async function sendPasswordReset(email) {
  const client = await getClient();
  const { error } = await client.auth.resetPasswordForEmail(email, { redirectTo: `${location.origin}/login` });
  if (error) throw error;
}

export async function signOut() {
  const client = await getClient();
  await client.auth.signOut();
}

export function friendlyAuthError(error) {
  const code = error?.code || "";
  const message = String(error?.message || "");
  if (code === "invalid_credentials" || /invalid login/i.test(message)) return "Email or password is incorrect.";
  if (code === "email_not_confirmed" || /not confirmed/i.test(message)) return "Please confirm your email first. Check your inbox.";
  if (code === "user_already_exists" || /already registered/i.test(message)) return "An account with this email already exists. Try signing in.";
  if (code === "weak_password" || /password/i.test(message) && /least|weak/i.test(message)) return "Password is too weak. Use at least 8 characters.";
  if (code === "over_request_rate_limit" || error?.status === 429) return "Too many attempts. Please wait a minute and try again.";
  if (error instanceof TypeError || /fetch|network/i.test(message)) return "Network problem. Check your connection and try again.";
  return "Something went wrong. Please try again.";
}
