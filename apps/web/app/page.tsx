import Link from "next/link";

export default function Home() {
  return <main className="mx-auto flex min-h-screen max-w-3xl flex-col justify-center p-8"><h1 className="text-3xl font-semibold">Harnessa workspace</h1><p className="mt-3 text-muted-foreground">Sign in or open an authorized project.</p><Link className="mt-6 w-fit rounded-lg bg-primary px-4 py-2 text-primary-foreground" href="/projects">Open projects</Link></main>;
}
