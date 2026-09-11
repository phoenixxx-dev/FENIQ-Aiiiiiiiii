"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { ApiError, api, tokens } from "@/lib/api";
import { t } from "@/i18n";

/** يعرض شاشة الدخول حتى وجود جلسة، ثم يعرض التطبيق. */
export function AuthGate({ children }: { children: React.ReactNode }) {
  const [ready, setReady] = useState(false);
  const [signedIn, setSignedIn] = useState(false);

  useEffect(() => {
    setSignedIn(Boolean(tokens.access));
    setReady(true);
  }, []);

  if (!ready) return null;
  if (!signedIn) return <AuthForm onDone={() => setSignedIn(true)} />;

  return (
    <>
      <header className="flex items-center justify-between border-b border-line bg-surface px-4 py-3">
        <nav className="flex items-center gap-4">
          <Link href="/" className="text-lg font-bold text-phoenix-700">{t("app.name")}</Link>
          <Link href="/" className="text-sm text-ink-600 hover:text-phoenix-700">{t("nav.files")}</Link>
          <Link href="/warehouses" className="text-sm text-ink-600 hover:text-phoenix-700">
            {t("nav.warehouses")}
          </Link>
        </nav>
        <button
          onClick={() => {
            tokens.clear();
            setSignedIn(false);
          }}
          className="rounded-lg px-3 py-1.5 text-sm text-ink-600 hover:bg-canvas"
        >
          {t("common.logout")}
        </button>
      </header>
      {children}
    </>
  );
}

function AuthForm({ onDone }: { onDone: () => void }) {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (mode === "login") await api.login(email, password);
      else await api.register(email, password);
      onDone();
    } catch (err) {
      // الرسالة من الخادم بالعربية — لا نخترع نصاً هنا
      setError(err instanceof ApiError ? err.message : t("common.error"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-sm flex-col justify-center px-5">
      <h1 className="mb-1 text-2xl font-bold text-phoenix-700">{t("app.name")}</h1>
      <p className="mb-7 text-sm text-ink-600">{t("app.tagline")}</p>

      <form onSubmit={submit} className="flex flex-col gap-3">
        <label className="flex flex-col gap-1.5">
          <span className="text-sm text-ink-700">{t("auth.email")}</span>
          <input
            type="email"
            required
            dir="ltr"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="rounded-lg border border-line bg-surface px-3 py-2.5 text-start outline-none focus:border-phoenix-500"
          />
        </label>

        <label className="flex flex-col gap-1.5">
          <span className="text-sm text-ink-700">{t("auth.password")}</span>
          <input
            type="password"
            required
            minLength={8}
            dir="ltr"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="rounded-lg border border-line bg-surface px-3 py-2.5 text-start outline-none focus:border-phoenix-500"
          />
          <span className="text-xs text-ink-400">{t("auth.passwordHint")}</span>
        </label>

        {error && (
          <p role="alert" className="rounded-lg bg-red-50 px-3 py-2 text-sm text-danger">
            {error}
          </p>
        )}

        <button
          type="submit"
          disabled={busy}
          className="mt-1 rounded-lg bg-phoenix-700 py-2.5 font-semibold text-white hover:bg-phoenix-600 disabled:opacity-60"
        >
          {busy ? t("auth.working") : mode === "login" ? t("auth.login") : t("auth.register")}
        </button>
      </form>

      <button
        onClick={() => {
          setMode(mode === "login" ? "register" : "login");
          setError(null);
        }}
        className="mt-4 text-sm text-ink-600 underline-offset-4 hover:underline"
      >
        {mode === "login" ? t("auth.switchToRegister") : t("auth.switchToLogin")}
      </button>
    </main>
  );
}
