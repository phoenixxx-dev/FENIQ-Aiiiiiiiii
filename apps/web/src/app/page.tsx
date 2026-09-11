"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";
import { AuthGate } from "@/components/AuthGate";
import { Upload } from "@/components/Upload";
import { api } from "@/lib/api";
import { t, tCount } from "@/i18n";

export default function Home() {
  return (
    <AuthGate>
      <Datasets />
    </AuthGate>
  );
}

function Datasets() {
  const { data, isLoading, refetch } = useQuery({
    queryKey: ["datasets"],
    queryFn: api.listDatasets,
  });

  return (
    <main className="mx-auto flex max-w-2xl flex-col gap-6 px-4 py-6">
      <Upload onDone={() => refetch()} />

      <section className="flex flex-col gap-3">
        <h2 className="text-sm font-semibold text-ink-600">{t("dataset.myFiles")}</h2>

        {isLoading && <p className="text-sm text-ink-400">{t("common.loading")}</p>}

        {!isLoading && (data?.length ?? 0) === 0 && (
          <div className="rounded-xl border border-line bg-surface p-6 text-center">
            <p className="font-medium">{t("dataset.empty")}</p>
            <p className="mt-1 text-sm text-ink-600">{t("dataset.emptyHint")}</p>
          </div>
        )}

        <ul className="flex flex-col gap-2">
          {data?.map((d) => (
            <li key={d.id} className="relative">
              <Link
                href={`/datasets/${d.id}`}
                className="flex items-center justify-between gap-3 rounded-xl border border-line bg-surface p-4 pe-14 hover:border-phoenix-500"
              >
                <span className="min-w-0">
                  <span className="block truncate font-medium"><bdi>{d.name}</bdi></span>
                  <span className="mt-0.5 block text-xs text-ink-600">
                    {d.status === "ready" ? (
                      <>
                        <bdi>{tCount("dataset.rows", d.row_count ?? 0)}</bdi> ·{" "}
                        <bdi>{tCount("dataset.columns", d.column_count ?? 0)}</bdi>
                      </>
                    ) : d.status === "failed" ? (
                      <span className="text-danger">{d.error_ar ?? t("job.failed")}</span>
                    ) : (
                      <span className="text-warn">{t("job.processing")}</span>
                    )}
                  </span>
                </span>
                <span className="flip shrink-0 text-ink-400">←</span>
              </Link>
              {/* زر الحذف خارج الرابط: زر داخل رابط يفتح الصفحة عند النقر */}
              <DeleteButton id={d.id} onDone={() => refetch()} />
            </li>
          ))}
        </ul>
      </section>
    </main>
  );
}


/** زر حذف بتأكيد داخلي — الحذف نهائي فلا يجوز أن يتم بنقرة واحدة عرضية. */
function DeleteButton({ id, onDone }: { id: string; onDone: () => void }) {
  const [asking, setAsking] = useState(false);
  const remove = useMutation({
    mutationFn: () => api.deleteDataset(id),
    onSuccess: () => {
      setAsking(false);
      onDone();
    },
  });

  if (!asking) {
    return (
      <button
        onClick={() => setAsking(true)}
        aria-label={t("dataset.delete")}
        className="absolute end-3 top-1/2 -translate-y-1/2 rounded-lg px-2 py-1 text-xs text-ink-400 hover:bg-canvas hover:text-danger"
      >
        {t("dataset.delete")}
      </button>
    );
  }

  return (
    <div className="absolute inset-0 flex items-center justify-between gap-2 rounded-xl border border-danger/40 bg-red-50 px-4">
      <span className="min-w-0 truncate text-xs">{t("dataset.confirmDelete")}</span>
      <span className="flex shrink-0 gap-2">
        <button
          onClick={() => setAsking(false)}
          className="rounded-lg border border-line bg-surface px-2 py-1 text-xs"
        >
          {t("common.cancel")}
        </button>
        <button
          onClick={() => remove.mutate()}
          disabled={remove.isPending}
          className="rounded-lg bg-danger px-2 py-1 text-xs font-semibold text-white disabled:opacity-40"
        >
          {remove.isPending ? t("dataset.deleting") : t("common.confirm")}
        </button>
      </span>
    </div>
  );
}
