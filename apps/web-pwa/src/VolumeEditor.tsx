import { uiText } from "./i18n";
import { Choice } from "./Choice";
import { useState } from "react";
import { Pencil, Plus } from "lucide-react";
import { api, enc, type Run } from "./model";
import { Label, Modal } from "./shared";
export type Volume = { name: string; read_only: boolean; path?: string };
export function VolumeEditor({
  node,
  volume,
  run,
  updated,
}: {
  node: string;
  volume?: Volume;
  run: Run;
  updated: (next?: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const close = () => {
    setOpen(false);
    setConfirm(false);
  };
  return (
    <>
      <button
        className="action"
        disabled={!node}
        aria-label={volume ? uiText("编辑挂载卷") : uiText("新增挂载卷")}
        onClick={() => setOpen(true)}
      >
        {volume ? <Pencil size={14} /> : <Plus size={14} />}
        {volume ? uiText("编辑卷") : uiText("新增卷")}
      </button>
      {open && (
        <Modal title={volume ? uiText("编辑挂载卷") : uiText("新增挂载卷")} close={close}>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              const f = new FormData(e.currentTarget);
              const name = volume?.name || String(f.get("name")).trim();
              run(async () => {
                await api(
                  "/nodes/" + enc(node) + "/volumes/" + enc(name),
                  "PUT",
                  { path: f.get("path"), read_only: f.get("mode") === "ro" },
                );
                close();
                updated(name);
              });
            }}
          >
            <Label name={uiText("卷名称")}>
              <input
                defaultValue={volume?.name || ""}
                name="name"
                readOnly={!!volume}
                required
              />
            </Label>
            <Label name={uiText("节点上的实际目录")}>
              <input name="path" defaultValue={volume?.path || ""} required />
            </Label>
            <Label name={uiText("访问权限")}>
              <Choice aria-label={uiText("访问权限")}
                name="mode"
                defaultValue={volume?.read_only === false ? "rw" : "ro"}
              >
                <option value="ro">{uiText("只读")}</option>
                <option value="rw">{uiText("读写")}</option>
              </Choice>
            </Label>
            <p className="caption">{uiText("目录须在节点管理员预授权的范围内。")}</p>
            <button className="primary">{uiText("保存挂载卷")}</button>
            {volume && (
              <button
                type="button"
                className="danger"
                onClick={() =>
                  confirm
                    ? run(async () => {
                        await api(
                          "/nodes/" +
                            enc(node) +
                            "/volumes/" +
                            enc(volume.name),
                          "DELETE",
                        );
                        close();
                        updated();
                      })
                    : setConfirm(true)
                }
              >
                {confirm ? uiText("确认解除卷映射") : uiText("解除卷映射")}
              </button>
            )}
          </form>
        </Modal>
      )}
    </>
  );
}
