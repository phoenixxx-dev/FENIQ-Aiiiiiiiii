"use client";

import ReactECharts from "echarts-for-react";
import type { Dashboard } from "@/lib/api";

type Spec = Dashboard["charts"][number];

/**
 * يترجم ChartSpec القادم من الخادم إلى إعدادات ECharts — لا أكثر.
 *
 * قاعدة الفصل (§5.8): هذا المكوّن لا يقرّر نوع الرسم ولا يحسب أي رقم؛
 * القرار والحساب تمّا في المحرك، وهنا مجرد عرض.
 */
export function Chart({ spec }: { spec: Spec }) {
  const data = (spec.data ?? []) as Array<Record<string, unknown>>;
  const xField = (spec.x as { field: string }).field;
  const yField = ((spec.y as Array<{ field: string }>)[0] ?? { field: "y" }).field;

  const labels = data.map((d) => String(d[xField] ?? ""));
  const values = data.map((d) => Number(d[yField] ?? 0));

  /**
   * أرقام مختصرة على المحاور: «2.5M» بدل «2,500,000».
   * بدونها تتراكب الأرقام الطويلة على شاشة الهاتف وتصير غير مقروءة إطلاقاً
   * (شوهد فعلياً: «0500,1,0001,5002,000»). التفاصيل الكاملة تبقى في التلميح.
   */
  const compact = (v: number): string => {
    const abs = Math.abs(v);
    if (abs >= 1e9) return `${(v / 1e9).toFixed(1)}B`;
    if (abs >= 1e6) return `${(v / 1e6).toFixed(1)}M`;
    if (abs >= 1e3) return `${(v / 1e3).toFixed(0)}K`;
    return String(v);
  };

  const valueAxis = {
    type: "value" as const,
    axisLabel: { formatter: compact, hideOverlap: true, fontSize: 10 },
  };

  const base = {
    grid: { top: 24, right: 12, bottom: 28, left: 52, containLabel: true },
    tooltip: {
      trigger: "axis" as const,
      confine: true,
      // التلميح يعرض الرقم كاملاً — الاختصار للمحور فقط
      valueFormatter: (v: number) => new Intl.NumberFormat("en-US").format(v),
    },
    textStyle: { fontFamily: "var(--font-ar), system-ui, sans-serif" },
    color: ["#E8590C", "#D9480F", "#F59E0B", "#0EA5E9", "#16A34A"],
  };

  let option: Record<string, unknown>;

  switch (spec.type) {
    case "donut":
      option = {
        ...base,
        tooltip: { trigger: "item", confine: true },
        series: [
          {
            type: "pie",
            radius: ["45%", "72%"],
            data: labels.map((name, i) => ({ name, value: values[i] })),
            label: { fontFamily: "inherit" },
          },
        ],
      };
      break;

    case "bar_horizontal":
      option = {
        ...base,
        // المحور الرأسي يميناً: الأنسب لقراءة الأسماء العربية بلا دوران
        xAxis: valueAxis,
        yAxis: {
          type: "category",
          data: labels,
          inverse: true,
          position: "right",
          axisLabel: { fontSize: 10, width: 110, overflow: "truncate" },
        },
        series: [{ type: "bar", data: values, barMaxWidth: 22 }],
      };
      break;

    case "line":
      option = {
        ...base,
        xAxis: { type: "category", data: labels, axisLabel: { fontSize: 10, hideOverlap: true } },
        yAxis: { ...valueAxis, position: "right" },
        series: [{ type: "line", data: values, smooth: true, showSymbol: data.length <= 40 }],
      };
      break;

    default:
      // bar / histogram / scatter / table → أعمدة رأسية كعرض افتراضي آمن
      option = {
        ...base,
        xAxis: { type: "category", data: labels, axisLabel: { fontSize: 10, hideOverlap: true } },
        yAxis: { ...valueAxis, position: "right" },
        series: [{ type: "bar", data: values, barMaxWidth: 32 }],
      };
  }

  // العنوان والسبب نصّان قادمان من الخادم — نطبّعهما صراحةً لأن الأنواع
  // المولّدة من OpenAPI تصفهما كقيم غير محدّدة النوع.
  const title = String(spec.title_ar ?? "");
  const reason = spec.reason_ar ? String(spec.reason_ar) : "";

  return (
    <figure className="rounded-xl border border-line bg-surface p-4">
      <figcaption className="mb-2 text-sm font-semibold">{title}</figcaption>
      <ReactECharts option={option} style={{ height: 240 }} notMerge lazyUpdate />
      {reason && <p className="mt-2 text-xs leading-relaxed text-ink-400">{reason}</p>}
    </figure>
  );
}
