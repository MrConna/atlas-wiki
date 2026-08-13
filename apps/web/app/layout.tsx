import type { Metadata } from "next";
import "./styles.css";

export const metadata: Metadata = {
  title: "Atlas Wiki",
  description: "Your knowledge, grounded and traceable.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
