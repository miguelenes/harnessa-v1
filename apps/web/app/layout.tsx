import type { Metadata } from "next";
import "./globals.css";
export const metadata: Metadata = { title: "Harnessa", description: "Self-hosted agent control plane", icons: { icon: "/icon.svg" } };
export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) { return <html lang="en"><body>{children}</body></html>; }
