export async function pages<T extends { id: string }>(base: string, path: string): Promise<T[]> {
  const rows: T[] = [];
  let cursor = "";
  while (true) {
    const separator = path.includes("?") ? "&" : "?";
    const response = await fetch(`${base}/api/v1${path}${separator}limit=100${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`, { credentials: "include" });
    if (!response.ok) throw new Error(`Не удалось загрузить данные (${response.status}).`);
    const page: T[] = await response.json();
    rows.push(...page);
    if (page.length < 100) return rows;
    cursor = page[page.length - 1].id;
  }
}
