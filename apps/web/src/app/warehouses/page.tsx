"use client";

/**
 * المستودعات ومفاتيح أجهزتها — ما يحتاجه صاحب المستودع ليشغّل وكيل FeniqSync.
 *
 * ثلاثة أشياء فقط: مستودع ← مفتاح لجهازه (يظهر مرة واحدة) ← هل المزامنة تصل؟
 * المفتاح الصريح لا يُحفظ في أي حالة دائمة بالواجهة: يعيش في ذاكرة المكوّن
 * حتى يضغط المستخدم «تم»، ثم يختفي — الخادم نفسه لا يملك نسخة منه.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { formatDistanceToNow } from "date-fns";
import { ar } from "date-fns/locale";
import { useState } from "react";
import { AuthGate } from "@/components/AuthGate";
import { ApiError, api, type TokenOut, type WarehouseOut } from "@/lib/api";
import { t, tCount } from "@/i18n";

// مزامنة يومية متوقَّعة + هامش ساعتين قبل اعتبار المستودع متأخّراً
const STALE_AFTER_MS = 26 * 60 * 60 * 1000;

const ago = (iso: string) =>
  formatDistanceToNow(new Date(iso), { addSuffix: true, locale: ar });

export default function WarehousesPage() {
  return (
    <AuthGate>
      <Warehouses />
    </AuthGate>
  );
}

function Warehouses() {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({ queryKey: ["warehouses"], queryFn: api.listWarehouses });
  const [name, setName] = useState("");
  const create = useMutation({
    mutationFn: () => api.createWarehouse(name.trim()),
    onSuccess: () => {
      setName("");
      qc.invalidateQueries({ queryKey: ["warehouses"] });
    },
  });

  return (
    <main className="mx-auto flex max-w-2xl flex-col gap-6 px-4 py-6">
      <section>
        <h1 className="text-xl font-bold">{t("warehouse.title")}</h1>
        <p className="mt-1 text-sm text-ink-600">{t("warehouse.intro")}</p>
      </section>

      <TelegramCard />

      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (name.trim()) create.mutate();
        }}
        className="flex gap-2"
      >
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder={t("warehouse.namePlaceholder")}
          maxLength={255}
          className="min-w-0 flex-1 rounded-lg border border-line bg-surface px-3 py-2.5 outline-none focus:border-phoenix-500"
        />
        <button
          disabled={!name.trim() || create.isPending}
          className="shrink-0 rounded-lg bg-phoenix-600 px-4 py-2.5 text-sm font-semibold text-white disabled:opacity-40"
        >
          {create.isPending ? t("warehouse.adding") : t("warehouse.add")}
        </button>
      </form>
      <ErrorLine error={create.error} />

      {isLoading && <p className="text-sm text-ink-400">{t("common.loading")}</p>}

      {!isLoading && (data?.length ?? 0) === 0 && (
        <div className="rounded-xl border border-line bg-surface p-6 text-center">
          <p className="font-medium">{t("warehouse.empty")}</p>
          <p className="mt-1 text-sm text-ink-600">{t("warehouse.emptyHint")}</p>
        </div>
      )}

      <ul className="flex flex-col gap-4">
        {data?.map((w) => <WarehouseCard key={w.id} w={w} />)}
      </ul>
    </main>
  );
}

function WarehouseCard({ w }: { w: WarehouseOut }) {
  const stale = w.last_sync_at && Date.now() - new Date(w.last_sync_at).getTime() > STALE_AFTER_MS;
  return (
    <li className="flex flex-col gap-4 rounded-xl border border-line bg-surface p-4">
      <div>
        <h2 className="font-semibold"><bdi>{w.name}</bdi></h2>
        <p className="mt-0.5 text-xs text-ink-600">
          {w.active_item_count > 0 && (
            <>
              <bdi>{tCount("warehouse.items", w.active_item_count)}</bdi>
              {" · "}
            </>
          )}
          {w.last_sync_at ? (
            <span className={stale ? "text-warn" : undefined}>
              {t("warehouse.lastSync")} {ago(w.last_sync_at)}
              {stale && <> — {t("warehouse.stale")}</>}
            </span>
          ) : (
            t("warehouse.neverSynced")
          )}
        </p>
      </div>
      <Tokens warehouseId={w.id} />
      <Syncs warehouseId={w.id} />
    </li>
  );
}

function Tokens({ warehouseId }: { warehouseId: string }) {
  const qc = useQueryClient();
  const key = ["tokens", warehouseId];
  const { data } = useQuery({ queryKey: key, queryFn: () => api.listTokens(warehouseId) });
  const [label, setLabel] = useState("");
  const [fresh, setFresh] = useState<TokenOut | null>(null);
  const create = useMutation({
    mutationFn: () => api.createToken(warehouseId, label.trim() || null),
    onSuccess: (tok) => {
      setFresh(tok);
      setLabel("");
      qc.invalidateQueries({ queryKey: key });
    },
  });

  return (
    <section className="flex flex-col gap-2">
      <h3 className="text-sm font-semibold text-ink-600">{t("warehouse.tokens")}</h3>

      {fresh ? (
        <FreshToken token={fresh.token} onDone={() => setFresh(null)} />
      ) : (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            create.mutate();
          }}
          className="flex gap-2"
        >
          <input
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            placeholder={t("warehouse.tokenLabel")}
            maxLength={255}
            className="min-w-0 flex-1 rounded-lg border border-line px-3 py-2 text-sm outline-none focus:border-phoenix-500"
          />
          <button
            disabled={create.isPending}
            className="shrink-0 rounded-lg border border-phoenix-500 px-3 py-2 text-sm font-semibold text-phoenix-700 disabled:opacity-40"
          >
            {create.isPending ? t("warehouse.creating") : t("warehouse.newToken")}
          </button>
        </form>
      )}
      <ErrorLine error={create.error} />

      {data?.length === 0 && <p className="text-xs text-ink-400">{t("warehouse.noTokens")}</p>}
      <ul className="flex flex-col gap-1.5">
        {data?.map((tk) => (
          <li
            key={tk.token_id}
            className="flex items-center justify-between gap-2 rounded-lg bg-canvas px-3 py-2 text-xs"
          >
            <span className="min-w-0">
              <code dir="ltr" className="font-mono">{tk.prefix}…</code>
              {tk.label && <> · <bdi>{tk.label}</bdi></>}
              <span className="block text-ink-400">
                {tk.revoked_at
                  ? t("warehouse.revoked")
                  : tk.last_used_at
                    ? `${t("warehouse.lastUsed")} ${ago(tk.last_used_at)}`
                    : t("warehouse.unused")}
              </span>
            </span>
            {!tk.revoked_at && <RevokeButton warehouseId={warehouseId} tokenId={tk.token_id} />}
          </li>
        ))}
      </ul>
    </section>
  );
}

/** المفتاح الصريح: يُعرض مرة واحدة مع رابط المزامنة — كل ما يحتاجه إعداد الوكيل. */
function FreshToken({ token, onDone }: { token: string; onDone: () => void }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="flex flex-col gap-2 rounded-lg border border-warn/50 bg-amber-50 p-3">
      <p className="text-xs font-medium">{t("warehouse.tokenOnce")}</p>
      <code dir="ltr" className="break-all rounded bg-surface p-2 font-mono text-xs">{token}</code>
      <p className="text-xs text-ink-600">
        {t("warehouse.endpoint")}:{" "}
        <code dir="ltr" className="break-all font-mono">{api.syncEndpoint()}</code>
      </p>
      <div className="flex gap-2">
        <button
          onClick={async () => {
            await navigator.clipboard.writeText(token);
            setCopied(true);
          }}
          className="rounded-lg border border-line bg-surface px-3 py-1.5 text-xs"
        >
          {copied ? t("warehouse.copied") : t("warehouse.copy")}
        </button>
        <button
          onClick={onDone}
          className="rounded-lg bg-phoenix-600 px-3 py-1.5 text-xs font-semibold text-white"
        >
          {t("warehouse.done")}
        </button>
      </div>
    </div>
  );
}

/** الإلغاء يوقف جهازاً حقيقياً عن المزامنة — تأكيد قبل التنفيذ. */
function RevokeButton({ warehouseId, tokenId }: { warehouseId: string; tokenId: string }) {
  const qc = useQueryClient();
  const [asking, setAsking] = useState(false);
  const revoke = useMutation({
    mutationFn: () => api.revokeToken(warehouseId, tokenId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["tokens", warehouseId] }),
  });

  if (!asking)
    return (
      <button
        onClick={() => setAsking(true)}
        className="shrink-0 rounded px-2 py-1 text-ink-400 hover:text-danger"
      >
        {t("warehouse.revoke")}
      </button>
    );

  return (
    <span className="flex shrink-0 flex-col items-end gap-1">
      <span className="text-danger">{t("warehouse.confirmRevoke")}</span>
      <span className="flex gap-1.5">
        <button onClick={() => setAsking(false)} className="rounded border border-line px-2 py-1">
          {t("common.cancel")}
        </button>
        <button
          onClick={() => revoke.mutate()}
          disabled={revoke.isPending}
          className="rounded bg-danger px-2 py-1 font-semibold text-white disabled:opacity-40"
        >
          {t("warehouse.confirmRevokeYes")}
        </button>
      </span>
    </span>
  );
}

function Syncs({ warehouseId }: { warehouseId: string }) {
  const { data } = useQuery({
    queryKey: ["syncs", warehouseId],
    queryFn: () => api.listSyncs(warehouseId),
  });
  return (
    <section className="flex flex-col gap-1.5">
      <h3 className="text-sm font-semibold text-ink-600">{t("warehouse.syncs")}</h3>
      {data?.length === 0 && <p className="text-xs text-ink-400">{t("warehouse.noSyncs")}</p>}
      <ul className="flex flex-col gap-1 text-xs">
        {data?.map((s) => (
          <li key={s.sync_id} className="flex flex-wrap justify-between gap-x-3 text-ink-600">
            <span>{ago(s.received_at)}</span>
            <span>
              <bdi>{t("warehouse.added")} {s.stats.added}</bdi>
              {" · "}
              <bdi>{t("warehouse.priceChanged")} {s.stats.price_changed}</bdi>
              {" · "}
              <bdi>{t("warehouse.removed")} {s.stats.removed}</bdi>
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}

/**
 * ربط تيليغرام بخطوتين: رابط t.me يحمل رمزاً مؤقتاً ← «ضغطت Start» فيقرأ الخادم
 * رسائل البوت ويطابق الرمز. لا webhook ولا لصق أرقام محادثة يدوياً.
 */
function TelegramCard() {
  const qc = useQueryClient();
  const { data } = useQuery({ queryKey: ["telegram"], queryFn: api.telegramStatus });
  const [opened, setOpened] = useState(false);
  const [missed, setMissed] = useState(false);
  const link = useMutation({
    mutationFn: api.telegramLink,
    onSuccess: ({ url }) => {
      window.open(url, "_blank", "noopener");
      setOpened(true);
      setMissed(false);
    },
  });
  const verify = useMutation({
    mutationFn: api.telegramVerify,
    onSuccess: (st) => {
      qc.setQueryData(["telegram"], st);
      setMissed(!st.linked);
      if (st.linked) setOpened(false);
    },
  });
  const unlink = useMutation({
    mutationFn: api.telegramUnlink,
    onSuccess: () => qc.invalidateQueries({ queryKey: ["telegram"] }),
  });

  if (!data) return null;
  return (
    <section className="flex flex-col gap-2 rounded-xl border border-line bg-surface p-4">
      <h2 className="font-semibold">{t("telegram.title")}</h2>
      <p className="text-xs text-ink-600">{t("telegram.explain")}</p>
      {!data.configured ? (
        <p className="text-xs text-ink-400">{t("telegram.notConfigured")}</p>
      ) : data.linked ? (
        <div className="flex items-center justify-between gap-2">
          <span className="text-sm text-ok-700">{t("telegram.linked")}</span>
          <button
            onClick={() => unlink.mutate()}
            className="rounded px-2 py-1 text-xs text-ink-400 hover:text-danger"
          >
            {t("telegram.unlink")}
          </button>
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          <button
            onClick={() => link.mutate()}
            disabled={link.isPending}
            className="self-start rounded-lg bg-phoenix-600 px-4 py-2 text-sm font-semibold text-white disabled:opacity-40"
          >
            {t("telegram.connect")}
          </button>
          {opened && (
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <span>{t("telegram.step")}</span>
              <button
                onClick={() => verify.mutate()}
                disabled={verify.isPending}
                className="rounded-lg border border-phoenix-500 px-3 py-1.5 font-semibold text-phoenix-700 disabled:opacity-40"
              >
                {verify.isPending ? t("telegram.checking") : t("telegram.verify")}
              </button>
            </div>
          )}
          {missed && <p className="text-xs text-warn-700">{t("telegram.notYet")}</p>}
        </div>
      )}
      <ErrorLine error={link.error ?? verify.error} />
    </section>
  );
}

function ErrorLine({ error }: { error: unknown }) {
  if (!error) return null;
  return (
    <p className="text-sm text-danger">
      {error instanceof ApiError ? error.message : t("common.error")}
    </p>
  );
}
