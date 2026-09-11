"use client";

import { useQuery, keepPreviousData } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { t, tCount } from "@/i18n";

const PAGE_SIZE = 25;

/**
 * استعراض البيانات بصفحات من الخادم.
 *
 * لا نُنزّل الجدول كاملاً أبداً: ملف 100 ألف صف يقتل الهاتف ذاكرةً وبطاريةً.
 * الخادم يقصّ الصفحة ويُرجع العدد الكلي فقط.
 */
export function DataTable({ datasetId }: { datasetId: string }) {
  const [page, setPage] = useState(1);
  const [term, setTerm] = useState("");
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<{ by: string; desc: boolean } | null>(null);

  // تأخير قصير قبل السؤال: بلا هذا يذهب طلب لكل حرف يُكتب
  useEffect(() => {
    const timer = setTimeout(() => {
      setQuery(term.trim());
      setPage(1);
    }, 350);
    return () => clearTimeout(timer);
  }, [term]);

  const { data, isLoading, isError } = useQuery({
    queryKey: ["rows", datasetId, page, query, sort?.by, sort?.desc],
    queryFn: () =>
      api.getRows(datasetId, page, PAGE_SIZE, query, sort?.by ?? "", sort?.desc ?? false),
    // نُبقي الصفحة السابقة ظاهرة أثناء جلب التالية — بدونها يومض الجدول فارغاً
    placeholderData: keepPreviousData,
  });

  // أعمدة الوسم لا تُعرض كأعمدة: نلوّن الخلية الأصلية بدلاً منها.
  // عرضها كان يُظهر أسماء داخلية («_شاذ_الكمية») في جدول المستخدم.
  const flags = data?.outlier_flags ?? {};
  const flagColumns = new Set(Object.values(flags));
  const visibleColumns = (data?.columns ?? []).filter((c) => !flagColumns.has(c));

  if (isLoading && !data) {
    return <p className="text-sm text-ink-400">{t("common.loading")}</p>;
  }
  if (isError || !data) {
    return <p className="text-sm text-danger">{t("common.error")}</p>;
  }

  return (
    // aria-labelledby يعطي القسم اسماً يقرأه قارئ الشاشة — وهو أيضاً ما
    // يميّزه عن جداول أخرى في الصفحة (جدول أمثلة التنظيف مثلاً)
    <section className="flex flex-col gap-3" aria-labelledby="data-table-title">
      <h2 id="data-table-title" className="text-sm font-semibold text-ink-600">
        {t("dashboard.table")}
      </h2>

      {/* البحث يتم في الخادم: الصفحة لا تحمل إلا 25 صفاً، فالبحث محلياً
          كان سيفتّش في الظاهر لا في الملف */}
      <div className="flex gap-2">
        <input
          value={term}
          onChange={(e) => setTerm(e.target.value)}
          placeholder={t("dataset.search")}
          aria-label={t("dataset.search")}
          className="min-w-0 flex-1 rounded-lg border border-line px-3 py-2 text-sm outline-none focus:border-phoenix-500"
        />
        {term && (
          <button
            onClick={() => setTerm("")}
            className="shrink-0 rounded-lg border border-line px-3 text-xs text-ink-600"
          >
            {t("dataset.clearSearch")}
          </button>
        )}
      </div>

      {/* الجدول يمرّر أفقياً داخل حاويته — الصفحة نفسها لا تتحرك جانبياً.
          tabIndex=0 ضروري: بدونه لا يستطيع مستخدم لوحة المفاتيح تمريره
          إطلاقاً (مخالفة scrollable-region-focusable كشفها axe-core). */}
      {data.total_rows === 0 && (
        <p className="rounded-xl border border-line bg-surface p-4 text-center text-sm text-ink-600">
          {t("dataset.noMatch")}
        </p>
      )}

      <div
        hidden={data.total_rows === 0}
        tabIndex={0}
        role="group"
        aria-labelledby="data-table-title"
        className="overflow-x-auto rounded-xl border border-line bg-surface focus:outline-2 focus:outline-offset-2 focus:outline-phoenix-500"
        // contain: paint ليس تجميلاً. الجدول عرضه 1133px داخل صندوق 343px،
        // و`overflow-x: auto` يقصّه بصرياً — لكن في صفحة RTL يبقى فائضه
        // محسوباً في عرض تمرير المستند نفسه، فتنزلق **الصفحة كلها** 197
        // بكسل على شاشة هاتف: عناوين وأزرار تخرج من الشاشة بلا سبب ظاهر.
        // قِيس فعلياً: overflow:hidden وbody/html/main لم تُصلحه، وهذا أصلحه.
        style={{ contain: "paint" }}
      >
        <table className="w-full min-w-max text-start text-xs">
          <thead>
            <tr className="border-b border-line bg-canvas">
              {visibleColumns.map((c, ci) => {
                const active = sort?.by === c;
                // العمود الأول مثبَّت (الخطة §8.7): على شاشة هاتف بعرض 375
                // بكسل وجدول فيه 27 عموداً، التمرير يميناً يُفقد المستخدم
                // أي صفٍّ ينظر إليه. start-0 لا left-0: في RTL البداية يمين.
                const pinned = ci === 0;
                return (
                  <th
                    key={c}
                    scope="col"
                    aria-sort={active ? (sort.desc ? "descending" : "ascending") : "none"}
                    className={`whitespace-nowrap px-3 py-2 text-start font-semibold text-ink-700 ${
                      pinned ? "sticky start-0 z-20 border-e border-line bg-canvas" : ""
                    }`}
                  >
                    {/* الفرز يتم في الخادم: ترتيب 25 صفاً معروضاً يرتّب
                        الظاهر لا الملف — وهو أسوأ من غياب الفرز */}
                    <button
                      onClick={() => {
                        setSort(active && !sort.desc ? { by: c, desc: true } : { by: c, desc: false });
                        setPage(1);
                      }}
                      aria-label={active && !sort.desc ? t("dataset.sortDesc") : t("dataset.sortAsc")}
                      className="flex items-center gap-1 font-semibold hover:text-phoenix-700"
                    >
                      <bdi>{c}</bdi>
                      <span aria-hidden className="num text-[10px] text-ink-400">
                        {active ? (sort.desc ? "▼" : "▲") : "↕"}
                      </span>
                    </button>
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {data.rows.map((row, i) => (
              <tr key={i} className="border-b border-line last:border-b-0">
                {visibleColumns.map((c, ci) => (
                  <Cell
                    key={c}
                    value={(row as Record<string, unknown>)[c]}
                    pinned={ci === 0}
                    outlier={Boolean(
                      flags[c] && (row as Record<string, unknown>)[flags[c]],
                    )}
                  />
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="flex items-center justify-between gap-3 text-xs text-ink-600">
        <button
          onClick={() => setPage((p) => Math.max(1, p - 1))}
          disabled={page <= 1}
          className="rounded-lg border border-line px-3 py-1.5 disabled:opacity-40"
        >
          <span className="flip inline-block">←</span>
        </button>

        <span>
          {t("common.page")} <span className="num">{data.page}</span> /{" "}
          <span className="num">{data.total_pages}</span> ·{" "}
          <bdi>{tCount("dataset.rows", data.total_rows)}</bdi>
        </span>

        <button
          onClick={() => setPage((p) => Math.min(data.total_pages, p + 1))}
          disabled={page >= data.total_pages}
          className="rounded-lg border border-line px-3 py-1.5 disabled:opacity-40"
        >
          <span className="flip inline-block">→</span>
        </button>
      </div>
    </section>
  );
}

function Cell({
  value,
  outlier = false,
  pinned = false,
}: {
  value: unknown;
  outlier?: boolean;
  pinned?: boolean;
}) {
  // القيمة الشاذة تُلوَّن ولا تُحذف — القرار للمستخدم (الخطة §5.5).
  // الخلفية وحدها لا تكفي لمن لا يميّز الألوان، فنضيف عنواناً منطوقاً.
  //
  // الخلية المثبَّتة يجب أن تكون **معتمة**: بلا خلفية صريحة تمرّ بقية
  // الأعمدة من تحتها فيصير النص فوق النص. لذلك bg-surface هنا صراحةً،
  // وتبقى خلفية الشذوذ إن وُجدت (وهي معتمة أصلاً).
  const mark = outlier ? "bg-orange-50" : pinned ? "bg-surface" : "";
  const pin = pinned ? "sticky start-0 z-10 border-e border-line" : "";
  const title = outlier ? t("dataset.outlierCell") : undefined;

  if (value === null || value === undefined) {
    // الفراغ يُعرض كشرطة صريحة — «فارغ» و«صفر» ليسا نفس الشيء
    return <td className={`px-3 py-2 text-ink-400 ${mark} ${pin}`}>—</td>;
  }
  if (typeof value === "number") {
    return (
      <td className={`num whitespace-nowrap px-3 py-2 text-start ${mark} ${pin}`} title={title}>
        {new Intl.NumberFormat("en-US").format(value)}
        {outlier && <span className="sr-only"> ({title})</span>}
      </td>
    );
  }
  if (typeof value === "boolean") {
    return <td className={`px-3 py-2 ${mark} ${pin}`}>{String(value)}</td>;
  }
  return (
    <td className={`max-w-[220px] truncate px-3 py-2 ${mark} ${pin}`} title={title}>
      <bdi>{String(value)}</bdi>
    </td>
  );
}
