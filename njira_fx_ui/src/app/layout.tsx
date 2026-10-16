import type { Metadata } from "next";
import "./globals.css";
import QueryProvider from "@/components/providers/QueryProvider";

export const metadata: Metadata = {
  title: "Auto-FX Terminal",
  description: "Institutional Quantitative Trading Terminal Engine",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body className="antialiased bg-[#05080f]">
        <QueryProvider>
          {children}
        </QueryProvider>
      </body>
    </html>
  );
}
