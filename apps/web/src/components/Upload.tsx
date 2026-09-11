"use client";

import { useRouter } from "next/navigation";
import { useRef, useState } from "react";
import { ApiError, api, type JobOut } from "@/lib/api";
import { t } from "@/i18n";

type Phase = "idle" | "uploading" | "processing" | "failed";

export function Upload({ onDone }: { onDone: () => void }) {
  const router = useRouter();
  const inputRef = useRef<HTMLInputElement>(null);
  const [phase, setPhase] = useState<Phase>("idle");
  const [percent, setPercent] = useState(0);
  const [job, setJob] = useState<JobOut | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);

  async function handle(file: File) {
    setError(null);
    setPhase("uploading");
    setPercent(0);
    try {
      const { dataset_id, job_id } = await api.upload(file, setPercent);
      setPhase("processing");

      // متابعة التقدّم: بث لحظي من الخادم (مع ارتداد تلقائي للـpolling).
      // النص العربي لكل مرحلة يأتي من الخادم فنعرضه كما هو.
      const final = await api.followJob(job_id, setJob);
      if (final.status === "succeeded") {
        onDone();
        router.push(`/datasets/${dataset_id}`);
      } else {
        setPhase("failed");
        setError(final.error_ar ?? t("job.failed"));
        // الملف الفاشل موجود في القائمة فعلاً — نحدّثها ليراه المستخدم
        // ويستطيع حذفه، بدل أن يبقى مخفياً حتى إعادة تحميل الصفحة.
        onDone();
      }
    } catch (err) {
      setPhase("failed");
      setError(err instanceof ApiError ? err.message : t("common.error"));
    }
  }

  if (phase === "uploading" || phase === "processing") {
    const label = phase === "uploading" ? t("upload.uploading") : (job?.stage_ar ?? t("job.processing"));
    const value = phase === "uploading" ? percent : (job?.progress ?? 0);
    return (
      <section className="rounded-xl border border-line bg-surface p-5">
        <p className="mb-3 text-sm font-medium text-ink-700">
          {label} <span className="num text-ink-400">{value}%</span>
        </p>
        <div className="h-2 overflow-hidden rounded-full bg-canvas">
          <div
            className="h-full rounded-full bg-phoenix-500 transition-[width] duration-500"
            style={{ width: `${Math.max(value, 3)}%` }}
            role="progressbar"
            aria-valuenow={value}
            aria-valuemin={0}
            aria-valuemax={100}
          />
        </div>
      </section>
    );
  }

  return (
    <section>
      <div
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          const f = e.dataTransfer.files?.[0];
          if (f) handle(f);
        }}
        onClick={() => inputRef.current?.click()}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") inputRef.current?.click();
        }}
        role="button"
        tabIndex={0}
        className={`flex cursor-pointer flex-col items-center gap-2 rounded-xl border-2 border-dashed p-8 text-center transition-colors focus:outline-2 focus:outline-offset-2 focus:outline-phoenix-500 ${
          dragging ? "border-phoenix-500 bg-phoenix-50" : "border-line bg-surface hover:border-phoenix-500"
        }`}
      >
        <span className="text-base font-semibold">{t("upload.title")}</span>
        <span className="text-sm text-ink-600">{t("upload.drop")}</span>
        <span className="num text-xs text-ink-400">{t("upload.formats")}</span>
      </div>

      <input
        ref={inputRef}
        type="file"
        hidden
        accept=".xlsx,.xls,.csv,.json,.xml,.html,.htm"
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) handle(f);
          e.target.value = "";
        }}
      />

      {error && (
        <p role="alert" className="mt-3 rounded-lg bg-red-50 px-3 py-2 text-sm text-danger">
          {error}
        </p>
      )}
    </section>
  );
}
