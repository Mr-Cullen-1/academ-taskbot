import type { Metadata } from "next";
import Script from "next/script";
import TaskbotApp from "./TaskbotApp";
import "./globals.css";

export const metadata: Metadata = {
  title: "Taskbot",
  description: "Telegram Taskbot Mini App",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    // Telegram's beforeInteractive SDK sets viewport variables on <html> before React hydrates.
    <html lang="ru" suppressHydrationWarning>
      <body>
        <Script src="https://telegram.org/js/telegram-web-app.js?63" strategy="beforeInteractive" />
        <TaskbotApp>{children}</TaskbotApp>
      </body>
    </html>
  );
}

