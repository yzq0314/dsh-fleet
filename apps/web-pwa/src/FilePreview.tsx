import { uiText } from "./i18n";
import { useEffect, useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import Papa from "papaparse";
import { ZoomIn, ZoomOut } from "lucide-react";
import { Modal } from "./shared";
import { enc } from "./model";

export function fileEndpoint(
  node: string,
  volume: string,
  path: string,
  action: "preview" | "download",
) {
  return (
    "/api/nodes/" +
    enc(node) +
    "/volumes/" +
    enc(volume) +
    "/" +
    action +
    "?path=" +
    enc(path)
  );
}
type PreviewData =
  | { image: string }
  | { text: string; format: "csv" | "markdown" | "plain" }
  | { message: string };
function CsvTable({ text }: { text: string }) {
  const { data, errors, meta } = useMemo(
    () =>
      Papa.parse<string[]>(text, {
        preview: 101,
        skipEmptyLines: "greedy",
        delimiter: ",",
      }),
    [text],
  );
  if (errors.length)
    return (
      <div>
        <p>{uiText("CSV 格式无法解析，显示原文。")}</p>
        <pre>{text}</pre>
      </div>
    );
  const rows = data.slice(0, 100);
  const columns = Math.min(24, Math.max(0, ...rows.map((row) => row.length)));
  return (
    <>
      <div
        className="csv-scroll"
        role="region"
        aria-label={uiText("CSV 表格")}
        tabIndex={0}
      >
        <table>
          <thead>
            <tr>
              {Array.from({ length: columns }, (_, i) => (
                <th scope="col" key={i}>
                  {rows[0]?.[i] || (uiText("列") + " ") + (i + 1)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.slice(1).map((r, i) => (
              <tr key={i}>
                {Array.from({ length: columns }, (_, j) => (
                  <td key={j}>{r[j] || ""}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {(meta.truncated ||
        data.length > 100 ||
        rows.some((r) => r.length > 24)) && (
        <small>{uiText("预览最多显示 100 行、24 列。可下载完整文件。")}</small>
      )}
    </>
  );
}
export function FilePreview({
  node,
  volume,
  path,
}: {
  node: string;
  volume: string;
  path: string;
}) {
  const [data, setData] = useState<PreviewData>({ message: uiText("正在读取文件…") });
  const [zoom, setZoom] = useState(false);
  const [fullSize, setFullSize] = useState(false);
  const name = path.split("/").pop() || path;
  useEffect(() => {
    const controller = new AbortController();
    let objectUrl = "";
    setZoom(false);
    setFullSize(false);
    setData({ message: uiText("正在读取文件…") });
    (async () => {
      const r = await fetch(fileEndpoint(node, volume, path, "preview"), {
        signal: controller.signal,
        cache: "no-store",
      });
      if (!r.ok)
        throw Error(
          r.status === 413
            ? uiText("文件超过在线预览上限，请下载查看。")
            : uiText("预览失败 (") + r.status + ")",
        );
      const mime =
        r.headers.get("content-type")?.split(";")[0].toLowerCase() ?? "";
      let next: PreviewData;
      if (
        [
          "image/png",
          "image/jpeg",
          "image/gif",
          "image/webp",
          "image/svg+xml",
        ].includes(mime)
      ) {
        const blob = await r.blob();
        if (controller.signal.aborted) return;
        objectUrl = URL.createObjectURL(blob);
        next = { image: objectUrl };
      } else if (mime.startsWith("text/") || mime === "application/json") {
        const text = await r.text();
        next = {
          text,
          format:
            /\.csv$/i.test(path) || mime === "text/csv"
              ? "csv"
              : /\.(md|markdown)$/i.test(path) || mime === "text/markdown"
                ? "markdown"
                : "plain",
        };
      } else {
        next = { message: uiText("此格式支持下载后查看。") };
      }
      if (!controller.signal.aborted) setData(next);
    })().catch((e) => {
      if (!controller.signal.aborted) setData({ message: e.message });
    });
    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [node, volume, path]);
  if ("message" in data) return <small role="status">{data.message}</small>;
  if ("image" in data)
    return (
      <>
        <button
          type="button"
          className="preview-picture"
          aria-label={(uiText("放大图片") + " ") + name}
          onClick={() => setZoom(true)}
        >
          <img src={data.image} alt={name} />
          <span>
            <ZoomIn size={15} />{uiText("放大")}</span>
        </button>
        {zoom && (
          <Modal title={name} close={() => setZoom(false)}>
            <div className="image-dialog">
              <button
                type="button"
                className="action"
                onClick={() => setFullSize((v) => !v)}
              >
                {fullSize ? <ZoomOut size={15} /> : <ZoomIn size={15} />}
                {fullSize ? uiText("适应屏幕") : uiText("查看原始尺寸")}
              </button>
              <div className={"image-pan " + (fullSize ? "original" : "")}>
                <img src={data.image} alt={name} />
              </div>
            </div>
          </Modal>
        )}
      </>
    );
  if (data.format === "csv") return <CsvTable text={data.text} />;
  if (data.format === "markdown")
    return (
      <SafeMarkdown text={data.text} />
    );
  return <pre className="plain-preview">{data.text}</pre>;
}

export function SafeMarkdown({text}:{text:string}) {
  return (<div className="markdown-preview">
        <ReactMarkdown
          skipHtml
          disallowedElements={[
            "img",
            "iframe",
            "script",
            "style",
            "video",
            "audio",
            "object",
            "embed",
          ]}
          components={{ a: ({ children }) => <span>{children}</span> }}
        >
          {text}
        </ReactMarkdown>
      </div>);
}
