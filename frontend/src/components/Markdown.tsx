// Markdown written by people (workflow record text and string values in a record body).
// Raw HTML in the source is shown as text, never rendered, and unsafe link protocols are
// dropped. Links to a path on this site ("/runs/…") open in the app; others in a new tab.
import type { ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { Link } from "react-router-dom";
import { cn } from "@/lib/utils";

const components: Components = {
  a: ({ href, children }) =>
    href?.startsWith("/") && !href.startsWith("//") ? (
      <Link to={href} className="text-primary underline underline-offset-2">
        {children}
      </Link>
    ) : (
      <a
        href={href}
        target="_blank"
        rel="noopener noreferrer"
        className="text-primary underline underline-offset-2"
      >
        {children}
      </a>
    ),
  h1: ({ children }) => (
    <h3 className="mt-3 text-base font-semibold first:mt-0">{children}</h3>
  ),
  h2: ({ children }) => (
    <h4 className="mt-3 text-sm font-semibold first:mt-0">{children}</h4>
  ),
  h3: ({ children }) => (
    <h5 className="mt-2 text-sm font-medium first:mt-0">{children}</h5>
  ),
  h4: ({ children }) => (
    <h6 className="mt-2 text-sm font-medium first:mt-0">{children}</h6>
  ),
  h5: ({ children }) => (
    <h6 className="mt-2 text-sm font-medium first:mt-0">{children}</h6>
  ),
  h6: ({ children }) => (
    <h6 className="mt-2 text-sm font-medium first:mt-0">{children}</h6>
  ),
  p: ({ children }) => <p className="my-2 first:mt-0 last:mb-0">{children}</p>,
  ul: ({ children }) => (
    <ul className="my-2 list-disc pl-5 first:mt-0 last:mb-0">{children}</ul>
  ),
  ol: ({ children }) => (
    <ol className="my-2 list-decimal pl-5 first:mt-0 last:mb-0">{children}</ol>
  ),
  li: ({ children }) => <li className="my-0.5">{children}</li>,
  blockquote: ({ children }) => (
    <blockquote className="my-2 border-l-2 pl-3 text-muted-foreground">
      {children}
    </blockquote>
  ),
  pre: ({ children }) => (
    <pre className="my-2 max-h-96 overflow-auto rounded-md bg-muted p-3 text-xs [&_code]:bg-transparent [&_code]:p-0">
      {children}
    </pre>
  ),
  code: ({ children }) => (
    <code className="rounded bg-muted px-1 py-0.5 font-mono text-[0.85em]">
      {children}
    </code>
  ),
  table: ({ children }) => (
    <div className="my-2 overflow-x-auto">
      <table className="text-xs">{children}</table>
    </div>
  ),
  th: ({ children }) => (
    <th className="border px-2 py-1 text-left font-medium">{children}</th>
  ),
  td: ({ children }) => (
    <td className="border px-2 py-1 align-top">{children}</td>
  ),
  hr: () => <hr className="my-3" />,
  img: ({ src, alt }) => (
    <a
      href={typeof src === "string" ? src : undefined}
      target="_blank"
      rel="noopener noreferrer"
    >
      {alt || String(src ?? "")}
    </a>
  ),
};

export function Markdown({
  children,
  className,
}: {
  children: string;
  className?: string;
}) {
  return (
    <div
      className={cn("min-w-0 break-words text-sm", className)}
      data-testid="markdown"
    >
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {children}
      </ReactMarkdown>
    </div>
  );
}

/**
 * A JSON value shown for reading: an object's keys as labels with their values below,
 * arrays as lists, strings as Markdown, other scalars as they are. The same rules apply
 * to every record kind; the raw JSON stays one click away.
 */
export function ReadableJson({ value }: { value: unknown }): ReactNode {
  if (typeof value === "string") return <Markdown>{value}</Markdown>;
  if (value === null)
    return <span className="text-muted-foreground">null</span>;
  if (typeof value !== "object")
    return <span className="font-mono text-xs">{String(value)}</span>;
  if (Array.isArray(value)) {
    if (value.length === 0)
      return <span className="text-muted-foreground">[]</span>;
    return (
      <ol className="grid gap-1.5 border-l pl-3">
        {value.map((item, index) => (
          <li key={index} className="min-w-0">
            <ReadableJson value={item} />
          </li>
        ))}
      </ol>
    );
  }
  const entries = Object.entries(value);
  if (entries.length === 0)
    return <span className="text-muted-foreground">{"{}"}</span>;
  return (
    <dl className="grid min-w-0 gap-2">
      {entries.map(([key, item]) => (
        <div key={key} className="grid min-w-0 gap-0.5">
          <dt className="break-all text-xs font-medium text-muted-foreground">
            {key}
          </dt>
          <dd className="min-w-0 pl-2">
            <ReadableJson value={item} />
          </dd>
        </div>
      ))}
    </dl>
  );
}
