"""Format Fleet hardware notices using only fixed labels and validated numbers."""
import re


def monitor_notice(event):
    data = event.get("data") or {}
    if type(event.get("id")) is not int or event["id"] < 1 or not re.fullmatch("[a-f0-9]{16}", str(data.get("id", ""))):
        return None
    kind = data.get("kind")
    if kind == "disk" and type(data.get("usage")) in (int, float) and 0 <= data["usage"] <= 100:
        title = "监控分区占用 " + str(data["usage"]) + "%"
    elif kind == "gpu" and type(data.get("gpu")) is int and 0 <= data["gpu"] < 64:
        title = "GPU " + str(data["gpu"]) + " 已连续三次空闲"
    elif kind in ("offline", "failed"):
        title = {"offline": "节点离线", "failed": "节点任务失败"}[kind]
    else:
        return None
    return "Fleet · " + title + "\n在 Fleet 控制台查看详情。"
