"use client";

/**
 * بطاقات الأعمدة — الخطة §8.5.
 *
 * لماذا تستحق شاشة؟ لأن المستخدم يحتاج أن يعرف **كيف قرأ فينيق ملفه** قبل
 * أن يثق بأرقامه: هل هذا العمود تاريخ أم نص؟ كم قيمة فارغة فيه؟ عمود فيه
 * 40% فراغات يجعل كل تحليل زمني فوقه ناقصاً — وإخفاء ذلك خداع بالصمت.
 */
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, type ColumnProfile } from "@/lib/api";
import { t, tCount } from "@/i18n";

function typeLabel(kind: string): string {
  const label = t(`profile.types.${kind}`);
  // المفتاح المفقود يعود كما هو — نعرض النوع الخام بدل فراغ
  return label.startsWith("profile.") ? kind : label;
}

export function ColumnProfiles({ datasetId }: { datasetId: string }) {
  const [open, setOpen] = useState(false);
  const { data } = useQuery({
    queryKey: ["profile", datasetId],
    queryFn: () => api.getProfile(datasetId),
    enabled: open,
  });

  return (
    <section className="flex flex-col gap-3 rounded-xl border border-line bg-surface p-4">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex items-center justify-between text-start"
      >
        <span className="text-sm font-semibold">{t("profile.title")}</span>
        <span className="text-xs text-phoenix-700">
          {open ? t("profile.hide") : t("profile.show")}
        </span>
      </button>

      {open && data && (
        <>
          {data.duplicate_row_count > 0 && (
            <p className="text-xs text-ink-600">
              <bdi>
                {tCount("profile.duplicateRows", data.duplicate_row_count)}
              </bdi>
            </p>
          )}
          <ul className="flex flex-col gap-2">
            {data.columns.map((c: ColumnProfile) => (
              <li key={c.name} className="rounded-lg border border-line p-3">
                <div className="flex items-baseline justify-between gap-2">
                  <span className="min-w-0 truncate text-xs font-medium" dir="auto">
                    <bdi>{c.name}</bdi>
                  </span>
                  <span className="shrink-0 rounded-full bg-canvas px-2 py-0.5 text-[10px] text-ink-600">
                    {typeLabel(c.inferred_type)}
                  </span>
                </div>

                <p className="mt-1 text-[11px] text-ink-600">
                  <bdi>{tCount("profile.unique", c.unique_count)}</bdi>
                  {c.null_pct > 0 && (
                    <>
                      {" · "}
                      <span className={c.null_pct >= 20 ? "text-warn-700" : ""}>
                        <span className="num">{Math.round(c.null_pct)}%</span>{" "}
                        {t("profile.empty")}
                      </span>
                    </>
                  )}
                </p>

                {c.min !== null && c.max !== null && (
                  <p className="mt-0.5 text-[11px] text-ink-400">
                    {/* «من x إلى y» بدل «x – y»: الشرطة بين رقمين في فقرة
                        عربية تترك أيَّ الطرفين الأدنى ملتبساً على القارئ */}
                    {t("profile.range")}: {t("profile.from")}{" "}
                    <span className="num">{c.min}</span> {t("profile.to")}{" "}
                    <span className="num">{c.max}</span>
                  </p>
                )}

                {(c.quality_issues ?? []).map((q, i) => (
                  <p key={i} className="mt-1 text-[11px] text-warn-700">
                    {q.message_ar}
                  </p>
                ))}
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
