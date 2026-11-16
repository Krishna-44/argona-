import "./globals.css";
import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Argona F&O",
  description: "Paper options scalper across US + crypto with India charges/tax and live trade signals.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
