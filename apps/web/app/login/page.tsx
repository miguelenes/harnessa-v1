"use client";

import type { FormEvent } from "react";
import { useState } from "react";
import { useRouter } from "next/navigation";
import {
  Alert,
  AlertDescription,
  Button,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  Input,
} from "@harnessa/ui-react";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8010";

export default function Login() {
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const router = useRouter();

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setSubmitting(true);

    try {
      const form = new FormData(event.currentTarget);
      const csrfResponse = await fetch(`${API}/auth/csrf`, { credentials: "include" });
      if (!csrfResponse.ok) {
        throw new Error(`Sign-in service is unavailable (${csrfResponse.status}).`);
      }

      const { csrf_token: csrfToken } = (await csrfResponse.json()) as { csrf_token: string };
      const response = await fetch(`${API}/auth/cookie/login`, {
        method: "POST",
        credentials: "include",
        headers: {
          "Content-Type": "application/x-www-form-urlencoded",
          "X-CSRF-Token": csrfToken,
        },
        body: new URLSearchParams({
          username: String(form.get("email")),
          password: String(form.get("password")),
        }),
      });

      if (!response.ok) {
        setError("Sign in failed. Check your credentials and try again.");
        return;
      }

      sessionStorage.setItem("csrf_token", csrfToken);
      router.push("/projects");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Sign-in service is unavailable.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center px-4 py-8">
      <Card className="w-full max-w-md">
        <CardHeader>
          <p className="text-xs font-semibold uppercase tracking-[.2em] text-[var(--accent-foreground)]">
            Harnessa
          </p>
          <CardTitle className="text-2xl">Sign in</CardTitle>
          <CardDescription>Use your installation account.</CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={(event) => void submit(event)} className="space-y-4">
            {error && (
              <Alert variant="destructive">
                <AlertDescription>{error}</AlertDescription>
              </Alert>
            )}
            <label htmlFor="email" className="grid gap-2 text-sm font-medium">
              Email
              <Input
                id="email"
                type="email"
                name="email"
                required
                autoComplete="username"
              />
            </label>
            <label htmlFor="password" className="grid gap-2 text-sm font-medium">
              Password
              <Input
                id="password"
                type="password"
                name="password"
                required
                autoComplete="current-password"
              />
            </label>
            <Button type="submit" className="w-full" disabled={submitting}>
              {submitting ? "Signing in…" : "Sign in"}
            </Button>
          </form>
        </CardContent>
      </Card>
    </main>
  );
}
