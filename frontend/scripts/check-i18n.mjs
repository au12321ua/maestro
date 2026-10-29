// Locale checks (N-18): every language has the same keys with the same {{variables}} and
// <tags>, and no text is written into the code: Chinese characters in a string, template
// or JSX text outside src/locales/ fail. Comments are not checked.
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import ts from "typescript";

const root = new URL("../src/", import.meta.url).pathname;
const locales = join(root, "locales");
const SOURCE = "zh-CN";
const PLURAL = /_(zero|one|two|few|many|other)$/;
const CJK = /[　-〿㐀-鿿＀-￯]/;
const problems = [];

function flatten(value, prefix, out) {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    for (const [key, inner] of Object.entries(value)) flatten(inner, prefix ? `${prefix}.${key}` : key, out);
  } else if (typeof value === "string") {
    out.set(prefix, value);
  } else {
    problems.push(`${prefix}: not a string`);
  }
}

/** Keys with plural forms merged: "n_one" and "n_other" are both "n". */
function load(language) {
  const out = new Map();
  for (const file of readdirSync(join(locales, language)).filter((f) => f.endsWith(".json")).sort()) {
    const flat = new Map();
    flatten(JSON.parse(readFileSync(join(locales, language, file), "utf8")), file.slice(0, -5), flat);
    for (const [key, text] of flat) {
      const base = key.replace(PLURAL, "");
      out.set(base, [...(out.get(base) ?? []), text]);
    }
  }
  return out;
}

const markers = (texts) =>
  [...new Set(texts.flatMap((t) => [...t.matchAll(/\{\{\s*([\w.]+)[^}]*\}\}|<\/?(\w+)\s*\/?>/g)].map((m) => m[1] ?? `<${m[2]}>`)))]
    .sort()
    .join(" ");

const languages = readdirSync(locales).filter((d) => statSync(join(locales, d)).isDirectory());
const source = load(SOURCE);
for (const language of languages.filter((l) => l !== SOURCE)) {
  const other = load(language);
  for (const key of source.keys()) if (!other.has(key)) problems.push(`${language}: missing ${key}`);
  for (const key of other.keys()) if (!source.has(key)) problems.push(`${language}: extra ${key}`);
  for (const [key, texts] of source) {
    // "count" may be dropped where one form reads well for every number (Chinese), never added.
    const want = markers(texts).split(" ").filter((m) => m && m !== "count");
    const have = other.has(key) ? markers(other.get(key)).split(" ").filter((m) => m && m !== "count") : want;
    if (want.join(" ") !== have.join(" "))
      problems.push(`${language}: ${key} has ${have.join(" ") || "nothing"}, ${SOURCE} has ${want.join(" ") || "nothing"}`);
  }
}

function* sources(dir) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (path === locales) continue;
    if (statSync(path).isDirectory()) yield* sources(path);
    else if (/\.tsx?$/.test(name) && !name.endsWith(".d.ts")) yield path;
  }
}

for (const path of sources(root)) {
  const text = readFileSync(path, "utf8");
  if (!CJK.test(text)) continue;
  const file = ts.createSourceFile(path, text, ts.ScriptTarget.Latest, true, path.endsWith("x") ? ts.ScriptKind.TSX : ts.ScriptKind.TS);
  const visit = (node) => {
    if (
      (ts.isStringLiteral(node) ||
        ts.isNoSubstitutionTemplateLiteral(node) ||
        ts.isTemplateHead(node) ||
        ts.isTemplateMiddle(node) ||
        ts.isTemplateTail(node) ||
        ts.isJsxText(node)) &&
      CJK.test(node.text)
    ) {
      const { line } = file.getLineAndCharacterOfPosition(node.getStart());
      problems.push(`${relative(root, path)}:${line + 1}: text in code: ${node.text.trim().slice(0, 40)}`);
    }
    ts.forEachChild(node, visit);
  };
  visit(file);
}

if (problems.length) {
  console.error(problems.join("\n"));
  console.error(`${problems.length} locale problem(s)`);
  process.exit(1);
}
console.log(`Locales consistent (${languages.join(", ")}: ${source.size} keys), no text in code.`);
