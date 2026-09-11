/**
 * نصوص الواجهة. قاعدة ذهبية #5: لا نص عربي مكتوب داخل المكوّنات — كلها هنا.
 * t("dashboard.kpis") بدل كتابة «المؤشرات» في JSX.
 */
import ar from "./ar.json";

type Dict = { [k: string]: string | Dict };

export function t(path: string): string {
  const parts = path.split(".");
  let node: string | Dict = ar as Dict;
  for (const p of parts) {
    if (typeof node === "string") return path;
    node = node[p];
    if (node === undefined) return path;   // المفتاح الناقص يظهر كما هو، لا فراغ صامت
  }
  return typeof node === "string" ? node : path;
}

/**
 * تمييز العدد العربي: المعدود يتغيّر أربع مرات لا مرّتين — مفرد، مثنى،
 * جمع (3–10)، ثم مفرد مجدَّداً من 11 فصاعداً.
 *
 * كانت الواجهة تكتب المفرد دائماً: «5 صف»، «4 صف مكرر بالكامل». الأعداد
 * الكبيرة تصحّ صدفةً، والصغيرة — وهي الأكثر ظهوراً — خطأ يراه كل قارئ عربي.
 * الصيغ الثلاث في ar.json لا في المكوّن (قاعدة ذهبية #5).
 */
export function tCount(path: string, n: number): string {
  const parts = path.split(".");
  let node: string | Dict = ar as Dict;
  for (const p of parts) {
    if (typeof node === "string") return path;
    node = node[p];
    if (node === undefined) return path;
  }
  const num = new Intl.NumberFormat("en-US").format(n);
  if (typeof node === "string") return `${num} ${node}`;   // مفتاح بصيغة واحدة
  const forms = node as unknown as {
    one: string; two: string; few: string; many?: string;
  };
  // صيغة الواحد **تُكتب بلا رقم** («صف واحد» لا «1 صف») — لكنها عبارة قائمة
  // بذاتها في الواجهة، فلا يكفي المفرد المجرّد: «قيمة مختلفة» وحدها ظهرت على
  // الشاشة فلم يعرف القارئ أهي واحدة أم رقم ضائع. لذلك `one` تحمل «واحدة»،
  // و`many` تحمل المفرد المجرّد الذي يلي عدداً كبيراً («11 قيمة مختلفة»).
  if (n === 1) return forms.one;
  if (n === 2) return forms.two;
  if (n >= 3 && n <= 10) return `${num} ${forms.few}`;
  return `${num} ${forms.many ?? forms.one}`;
}
