/* Prose — renders agent/markdown text as native, readable app typography.
   Used for workflow results, notes cards, and chat messages. No raw asterisks
   or mono-font walls ever again. */
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

export default function Prose({ children, size = "sm" }: { children: string; size?: "sm" | "md" }) {
  const base = size === "md" ? "text-[14px]" : "text-[13px]";
  return (
    <div className={`${base} leading-relaxed text-ink-2 min-w-0
      [&>*:first-child]:mt-0 [&>*:last-child]:mb-0
      [&_p]:my-1.5
      [&_strong]:text-ink [&_strong]:font-semibold
      [&_em]:text-ink-2
      [&_a]:text-blue-2 [&_a]:no-underline hover:[&_a]:underline
      [&_h1]:text-[15px] [&_h1]:font-semibold [&_h1]:text-ink [&_h1]:mt-3 [&_h1]:mb-1
      [&_h2]:text-[14px] [&_h2]:font-semibold [&_h2]:text-ink [&_h2]:mt-3 [&_h2]:mb-1
      [&_h3]:text-[13px] [&_h3]:font-semibold [&_h3]:text-ink [&_h3]:mt-2.5 [&_h3]:mb-1
      [&_ul]:my-1.5 [&_ul]:pl-4 [&_ul]:list-disc [&_ul]:space-y-1 [&_ul]:marker:text-ink-4
      [&_ol]:my-1.5 [&_ol]:pl-4 [&_ol]:list-decimal [&_ol]:space-y-1 [&_ol]:marker:text-ink-4
      [&_li]:leading-relaxed
      [&_code]:font-mono [&_code]:text-[0.92em] [&_code]:text-blue-2 [&_code]:bg-blue/10 [&_code]:px-1 [&_code]:py-px [&_code]:rounded
      [&_pre]:bg-[#0a1322] [&_pre]:border [&_pre]:border-line [&_pre]:rounded-lg [&_pre]:p-3 [&_pre]:my-2 [&_pre]:overflow-auto
      [&_pre_code]:bg-transparent [&_pre_code]:text-ink-2 [&_pre_code]:p-0
      [&_blockquote]:border-l-2 [&_blockquote]:border-line-2 [&_blockquote]:pl-3 [&_blockquote]:my-2 [&_blockquote]:text-ink-3
      [&_hr]:border-line [&_hr]:my-3
      [&_table]:my-2 [&_table]:w-full [&_table]:text-[12.5px]
      [&_th]:text-left [&_th]:text-[10.5px] [&_th]:uppercase [&_th]:tracking-wider [&_th]:text-ink-4 [&_th]:font-semibold [&_th]:pb-1 [&_th]:pr-3 [&_th]:border-b [&_th]:border-line-2
      [&_td]:py-1 [&_td]:pr-3 [&_td]:border-b [&_td]:border-line [&_td]:align-top`}>
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{children}</ReactMarkdown>
    </div>
  );
}
