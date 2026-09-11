/**
 * اختبارات المكوّنات: تركّز على القواعد التي يسهل خرقها بصمت.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { EvidenceDrawer } from "../EvidenceDrawer";
import { t } from "@/i18n";

describe("درج الأدلة", () => {
  const evidence = {
    metric_name: "sum(total_amount)",
    sql: 'SELECT SUM("الاجمالي") FROM dataset',
    source_columns: ["الاجمالي"],
    rows_in_scope: 481,
    rows_total: 481,
  };

  it("يعرض الاستعلام الفعلي لا نصاً ثابتاً", () => {
    render(<EvidenceDrawer evidence={evidence} onClose={() => {}} />);
    expect(screen.getByText(/SELECT SUM/)).toBeInTheDocument();
  });

  it("يعرض نطاق الصفوف كي يعرف المستخدم على ماذا حُسب الرقم", () => {
    render(<EvidenceDrawer evidence={evidence} onClose={() => {}} />);
    expect(screen.getAllByText("481").length).toBeGreaterThan(0);
  });

  it("الاستعلام يُعرض باتجاه LTR — وإلا انقلب ترتيبه داخل صفحة عربية", () => {
    const { container } = render(
      <EvidenceDrawer evidence={evidence} onClose={() => {}} />,
    );
    const sql = container.querySelector("dd[dir='ltr']");
    expect(sql?.textContent).toContain("SELECT");
  });

  it("لا يعرض شيئاً بلا دليل — لا درج فارغ", () => {
    const { container } = render(<EvidenceDrawer evidence={null} onClose={() => {}} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("نظام النصوص", () => {
  it("يُرجع النص العربي للمفتاح الموجود", () => {
    expect(t("dashboard.evidence")).toBe("كيف حُسب؟");
  });

  it("يُظهر المفتاح كما هو لو كان مفقوداً — لا فراغ صامت", () => {
    expect(t("لا.يوجد.هذا")).toBe("لا.يوجد.هذا");
  });
});

describe("قاعدة ذهبية #5: لا نص عربي داخل المكوّنات", () => {
  it("كل النصوص المعروضة تأتي من ملف النصوص", () => {
    // src/lib مشمول عمداً: ثلاث رسائل خطأ عربية كانت مكتوبة داخل
    // api.ts وتُعرض للمستخدم فعلاً — والحارس لم يكن يراها لأنه كان
    // يمسح المكوّنات وحدها. الحدّ هو «نص معروض»، لا «ملف .tsx».
    const roots = ["src/components", "src/app", "src/lib"];
    const offenders: string[] = [];

    const walk = (dir: string) => {
      for (const entry of readdirSync(dir, { withFileTypes: true })) {
        const full = join(dir, entry.name);
        if (entry.isDirectory()) {
          if (entry.name !== "__tests__") walk(full);
          continue;
        }
        if (!/\.tsx?$/.test(entry.name)) continue;
        if (full.includes("/i18n/")) continue;   // ملف النصوص نفسه
        // نتجاهل التعليقات: الشرح بالعربية داخل الكود مطلوب لا ممنوع.
        // نتتبّع كتلة {/* ... */} سطراً بسطر — فحص بداية السطر وحده كان
        // يعلن سطراً وسط تعليق مخالفةً (إنذار كاذب وقعنا فيه فعلاً).
        let inBlock = false;
        for (const line of readFileSync(full, "utf8").split("\n")) {
          const code = line.trim();
          if (inBlock) {
            if (code.includes("*/")) inBlock = false;
            continue;
          }
          if (code.startsWith("{/*") || code.startsWith("/*")) {
            if (!code.includes("*/")) inBlock = true;
            continue;
          }
          if (code.startsWith("//") || code.startsWith("*")) continue;
          // النص المعروض قد يأتي بأي من أشكال السلاسل الثلاثة، لا
          // بالمزدوجة وحدها: `تم` و'تم' كانتا تمرّان.
          if (
            /"[؀-ۿ]/.test(code) ||
            /'[؀-ۿ]/.test(code) ||
            /`[^`]*[؀-ۿ]/.test(code) ||
            />\s*[؀-ۿ]{3,}/.test(code)
          ) {
            offenders.push(`${full}: ${code.slice(0, 70)}`);
          }
        }
      }
    };
    roots.forEach(walk);

    expect(offenders).toEqual([]);
  });
});

describe("سجل التنظيف: إظهار غير المرئي", () => {
  it("يُظهر المسافات الطرفية كنقاط فلا تبدو القيمتان متطابقتين", async () => {
    const { visible } = await import("@/components/CleaningReview");
    expect(visible("شركة النور ")).toBe("شركة النور·");
    expect(visible("  اسم")).toBe("··اسم");
    expect(visible("شركة النور")).toBe("شركة النور");
    expect(visible(null)).toBe("");
    // القيمتان قبل/بعد يجب أن تختلفا بصرياً بعد المعالجة
    expect(visible("شركة النور ")).not.toBe(visible("شركة النور"));
  });

  it("يُظهر المحارف عديمة العرض — وإلا بدا «قبل» و«بعد» متطابقين", async () => {
    const { visible } = await import("@/components/CleaningReview");
    // مسافة صفرية العرض، علامة اتجاه، BOM — ثلاثتها لا تُرى على الشاشة
    expect(visible("شركة​النور")).toBe("شركة◌النور");
    expect(visible("شركة‏النور")).toBe("شركة◌النور");
    expect(visible("﻿النور")).toBe("◌النور");
    expect(visible("شركة​النور")).not.toBe(visible("شركةالنور"));
  });
});

describe("تمييز العدد العربي", () => {
  it("يستعمل الصيغ الأربع لا صيغتين", async () => {
    const { tCount } = await import("@/i18n");
    // «صف واحد» لا «صف» وحدها: العبارة تقف بذاتها في الواجهة، و«قيمة مختلفة»
    // المجرّدة ظهرت على الشاشة فلم يعرف القارئ أهي واحدة أم رقم ضائع.
    expect(tCount("dataset.rows", 1)).toBe("صف واحد");
    expect(tCount("dataset.rows", 2)).toBe("صفّان");
    expect(tCount("dataset.rows", 5)).toBe("5 صفوف");
    expect(tCount("dataset.rows", 10)).toBe("10 صفوف");
    expect(tCount("dataset.rows", 11)).toBe("11 صف");
    expect(tCount("dataset.rows", 4810)).toBe("4,810 صف");
    expect(tCount("profile.unique", 1)).toBe("قيمة مختلفة واحدة");
    expect(tCount("profile.unique", 12)).toBe("12 قيمة مختلفة");
  });

  it("مفتاح بصيغة واحدة لا ينكسر", async () => {
    const { tCount } = await import("@/i18n");
    // مفتاح نصّي عادي (بلا صيغ) يبقى مفهوماً بدل أن يظهر المسار
    expect(tCount("common.page", 3)).toBe("3 صفحة");
  });

  it("كل مفتاح يُستعمل مع tCount له الصيغ الثلاث", async () => {
    const ar = (await import("@/i18n/ar.json")).default as Record<string, unknown>;
    const used = ["dataset.rows", "dataset.columns", "cleaning.rowsRemoved",
                  "profile.duplicateRows", "profile.unique"];
    for (const path of used) {
      const node = path.split(".").reduce<unknown>(
        (n, k) => (n as Record<string, unknown>)?.[k], ar) as Record<string, string>;
      expect(Object.keys(node ?? {}).sort()).toEqual(["few", "many", "one", "two"]);
    }
  });
});

describe("سلامة الاتجاه في النصوص العربية", () => {
  it("لا نص عربي يحتوي مدى أرقام يلتبس اتجاهه", () => {
    // «0-9» داخل جملة عربية يُعرض «9-0» — عيب حقيقي ظهر على الشاشة.
    // الحارس يفحص ملف النصوص كله لا سطراً بعينه.
    const raw = readFileSync(join("src", "i18n", "ar.json"), "utf8");
    const offenders = raw
      .split("\n")
      .filter((line) => /[؀-ۿ]/.test(line) && /\d\s*-\s*\d/.test(line));
    expect(offenders).toEqual([]);
  });
});

describe("شكل الرسالة المختلطة", () => {
  it("لا رسالة عربية تنتهي بكلمة لاتينية ثم نقطة", () => {
    // «... إلى Excel أو CSV.» تُعرض «... أو .CSV»: النقطة محرف محايد في
    // آخر النص فتلتحق باتجاه الفقرة وتقفز أمام المقطع اللاتيني. عيب حقيقي
    // ظهر في شاشة الفشل. العلاج: أنهِ الجملة بكلمة عربية.
    const raw = readFileSync(join("src", "i18n", "ar.json"), "utf8");
    const offenders = raw
      .split("\n")
      .filter((line) => /[؀-ۿ]/.test(line))
      .filter((line) => /[A-Za-z][A-Za-z0-9]*[)\]»]?\s*\.\s*"\s*,?\s*$/.test(line));
    expect(offenders).toEqual([]);
  });
});

describe("العقد: لا نوع API مكتوب يدوياً", () => {
  it("كل نوع في عميل الـAPI مشتقّ من OpenAPI", () => {
    // ادّعاء في STATUS: «لا نوع مكتوب يدوياً». بلا حارس يبقى ادّعاءً —
    // ونوع مكتوب بيد الواجهة ينحرف عن الخادم بصمت عند أول تعديل.
    const src = readFileSync(join("src", "lib", "api.ts"), "utf8");
    const declared = [...src.matchAll(/^export type (\w+)\s*=\s*([\s\S]*?);$/gm)];
    expect(declared.length).toBeGreaterThan(5);

    const derived = new Set<string>();
    const offenders: string[] = [];
    for (const [, name, body] of declared) {
      const fromOpenApi = body.includes("paths[");
      const fromAnother = [...derived].some((d) => body.includes(d));
      if (fromOpenApi || fromAnother) derived.add(name);
      else offenders.push(`${name} = ${body.trim().slice(0, 60)}`);
    }
    expect(offenders).toEqual([]);
  });
});
