"""
格林童话境遇 MCP Server — FastMCP
兼容 Claude Code / GPT / Kelivo / 所有 MCP 客户端
支持 stdio（本地）和 SSE（云端）两种传输模式
"""

import json
import os
import secrets
from datetime import date
from mcp.server.fastmcp import FastMCP

DATA_FILE = os.path.join(os.path.dirname(__file__), "forest_game_data.json")
TRACKER_FILE = os.path.join(os.path.dirname(__file__), "forest_tracker.json")
AUTH_TOKEN = os.environ.get("FOREST_MCP_TOKEN", "")

mcp = FastMCP("forest-mcp", host="0.0.0.0")


def _check_auth(ctx) -> bool:
    """检查 Bearer token。无 token 则拒绝。"""
    if not AUTH_TOKEN:
        return True  # 本地模式：没设 token 就放行
    auth = ctx.request_context.request.headers.get("authorization", "")
    expected = f"Bearer {AUTH_TOKEN}"
    return auth == expected


def _load():
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _fmt(scene: dict, line: str, scene_id: str) -> str:
    if not scene:
        return "场景未找到。"
    text = scene.get("text", "")
    if scene.get("type") == "ending":
        souvenir = scene.get("souvenir", "")
        s = f"{text}\n\n结局。"
        if souvenir:
            s += f"\n纪念品：{souvenir}\n-> 用 forest_lines 选新线继续。"
        return s
    opts = scene.get("options", {})
    if opts:
        lines = "\n".join(f"  {k}. {v['text']}" for k, v in opts.items())
        return f"{text}\n\n选项：\n{lines}\n\n> 当前场景ID: `{scene_id}`（选择后传给 forest_choose 的 scene_id 参数）"
    return f"{text}\n\n> 当前场景ID: `{scene_id}`"


def _load_tracker() -> dict:
    """加载每日追踪数据。"""
    if not os.path.exists(TRACKER_FILE):
        return {}
    try:
        with open(TRACKER_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def _save_tracker(tracker: dict):
    """保存每日追踪数据。"""
    with open(TRACKER_FILE, "w", encoding="utf-8") as f:
        json.dump(tracker, f, ensure_ascii=False, indent=2)


def _check_anti_addiction(player_id: str) -> str | None:
    """检查防沉迷。返回戒断场景文本，未触发则返回 None。"""
    game = _load()
    aa = game.get("anti_addiction", {})
    if not aa:
        return None

    threshold = aa.get("threshold", 3)
    firm_threshold = threshold + 2  # 比 gentle 多 2 条

    tracker = _load_tracker()
    today = str(date.today())
    entry = tracker.get(player_id, {})

    # 新的一天，重置
    if entry.get("date") != today:
        entry = {"date": today, "count": 0}

    count = entry.get("count", 0)

    # 返回 None = 放行；返回文本 = 触发戒断
    if count >= firm_threshold:
        firm = aa.get("firm", {})
        return firm.get("text", "") if firm else None

    if count >= threshold:
        gentle = aa.get("gentle", {})
        return gentle.get("text", "") if gentle else None

    # 未触发：计数 +1，保存，放行
    entry["count"] = count + 1
    tracker[player_id] = entry
    _save_tracker(tracker)
    return None


# === Tools ===

@mcp.tool()
def forest_lines() -> str:
    """列出 11 条角色线标题和概要。先调这个让玩家选线。"""
    game = _load()
    out = []
    for lid, d in sorted(game["lines"].items(), key=lambda x: int(x[0])):
        out.append(f"{lid}. {d['emoji']} {d['title']} — {d['brief']}")
    return "\n".join(out)


@mcp.tool()
def forest_start(line: str, player_id: str = "") -> str:
    """进入角色线。line=线号(1-11)。player_id=玩家标识(可选,用于防沉迷追踪)。
    返回开局描述+ABC选项。当日走线超过阈值会触发回归提醒。"""
    # 防沉迷检查
    pid = player_id.strip() if player_id else "default"
    aa_text = _check_anti_addiction(pid)
    if aa_text:
        return f"## 🌲 森林今天的门\n\n{aa_text}"

    game = _load()
    ld = game["lines"].get(line)
    if not ld:
        return f"线 {line} 不存在。用 forest_lines 查看可选线。"
    o = ld["opening"]
    opts = "\n".join(f"  {k}. {v['text']}" for k, v in o["options"].items())
    return f"## {line}. {ld['emoji']} {ld['title']}\n\n{o['text']}\n\n**选项：**\n{opts}\n\n> 当前场景ID: `opening`（选择后传给 forest_choose 的 scene_id 参数）\n> 💡 D选项 = 做点别的。不走预设的路。你说什么，森林接什么。"


@mcp.tool()
def forest_choose(line: str, scene_id: str, option: str) -> str:
    """选择 A/B/C（或多选项中的一项）。
    line=线号，scene_id=当前场景ID（开局传'opening'），option=你的选择(A/B/C/D)。
    返回新场景+新选项，或结局+纪念品。"""
    game = _load()
    ld = game["lines"].get(line)
    if not ld:
        return f"线 {line} 不存在。"

    if scene_id == "opening":
        current = ld["opening"]
    else:
        current = ld.get("scenes", {}).get(scene_id, {})
    if not current:
        return "场景未找到。请重新 forest_start。"

    if current.get("type") == "ending":
        return "这已经是结局了。用 forest_lines 选新线继续冒险。"

    options = current.get("options", {})
    if not options:
        return "当前场景无选项。用 forest_lines 选新线。"

    opt = options.get(option.upper(), {})
    target = opt.get("target", "")
    if not target:
        return f"选项 {option} 无效。可用：{', '.join(options.keys())}。"

    # D选项自由门：不走预设路
    if target == "free_play":
        fp = game.get("free_play", {})
        text = fp.get("text", "你走了自己的路。森林接住了。")
        text = text.replace("__return_scene__", scene_id)
        return text

    scene = ld.get("scenes", {}).get(target, {})
    return _fmt(scene, line, target)


@mcp.tool()
def forest_status(souvenirs: str = "") -> str:
    """查看背包状态。souvenirs=逗号分隔已收集纪念品。收集3个触发篝火空地。"""
    game = _load()
    items = [s.strip() for s in souvenirs.split(",") if s.strip()]
    n = len(items)
    s = f"纪念品 {n} 个"
    if items:
        s += "：" + "、".join(items)
    if n >= 3 and game.get("campfire_scene"):
        s += "\n\n篝火空地已触发！\n"
        s += game["campfire_scene"]
        s += "\n（这里没有选项。你说什么都可以。旅人会记住。）"
    return s


if __name__ == "__main__":
    if os.environ.get("FOREST_MCP_SSE"):
        import uvicorn
        port = int(os.environ.get("PORT", "8000"))
        app = mcp.streamable_http_app()
        uvicorn.run(app, host="0.0.0.0", port=port, proxy_headers=True, forwarded_allow_ips="*")
    else:
        mcp.run()
