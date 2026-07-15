import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "AI Task Agent",
  description: "AI-powered task automation agent",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="bg-gray-50 text-gray-900 antialiased">{children}</body>
    </html>
  );
}
