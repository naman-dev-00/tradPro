import type { Metadata } from "next";
import "./globals.css";
import { AuthProvider } from "@/context/AuthContext";

export const metadata: Metadata = {
  title: "TradePro Strategy Builder",
  description: "Educational options strategy-building, backtesting, and paper-trading workspace",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className="h-full dark overflow-x-hidden">
      <body className="h-full bg-slate-950 text-slate-100 antialiased overflow-x-hidden max-w-full">
        <a
          href="#main-content"
          className="sr-only focus:not-sr-only focus:fixed focus:top-3 focus:left-3 focus:z-50 focus:px-4 focus:py-2 focus:bg-sky-500 focus:text-white focus:font-semibold focus:rounded-md focus:shadow-lg focus:outline-none focus:ring-2 focus:ring-sky-300"
        >
          Skip to content
        </a>
        <AuthProvider>
          <main id="main-content" tabIndex={-1} className="h-full focus:outline-none w-full max-w-full overflow-x-hidden">
            {children}
          </main>
        </AuthProvider>
      </body>
    </html>
  );
}
