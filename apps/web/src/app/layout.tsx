import type { Metadata } from "next";
import "./globals.css";
import { Providers } from "@/components/Providers";
import { t } from "@/i18n";

export const metadata: Metadata = {
  title: t("app.name"),
  description: t("app.tagline"),
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  // dir=rtl على مستوى المستند: كل الخصائص المنطقية بالـCSS تنقلب تلقائياً.
  //
  // الخط يُحمَّل برابط داخل المتصفح لا عبر next/font: الأخير يجلب الخط وقت
  // البناء، فيفشل البناء بالكامل في أي بيئة بلا وصول لـfonts.googleapis.com
  // (بيئة CI مغلقة مثلاً — وهو ما حصل فعلاً هنا). بهذه الطريقة البناء ينجح
  // دائماً، والخط يظهر عند المستخدم، ويعود للخط النظامي لو تعذّر تحميله.
  return (
    <html lang="ar" dir="rtl">
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="" />
        <link
          rel="stylesheet"
          href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+Arabic:wght@400;500;600;700&display=swap"
        />
      </head>
      <body className="antialiased">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
