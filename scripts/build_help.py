"""Build the AstrBot help image from the README command and schedule tables."""
import asyncio
from html import escape
from io import BytesIO
from pathlib import Path
import re
import sys

from PIL import Image
from playwright.async_api import async_playwright

REPO = Path(__file__).resolve().parents[1]


def help_html() -> str:
    text = (REPO / "README.md").read_text("utf-8")
    commands = text.split("## 指令\n", 1)[1].split("## 目录结构", 1)[0]
    blocks = []
    title, rows = "", []

    def flush():
        if rows:
            blocks.append(f"<section><h2>{escape(title)}</h2><table>{''.join(rows)}</table></section>")
        rows.clear()

    for line in commands.splitlines():
        if line.startswith(("### ", "## ")):
            flush()
            title = line.lstrip("# ")
        elif line.startswith("|"):
            columns = [column.strip() for column in line.strip("|").split("|")]
            if not columns or all(re.fullmatch(r"[-: ]+", c) for c in columns):
                continue
            cells = []
            for column in columns:
                value = re.sub(r"`([^`]+)`", r"<code>\1</code>", escape(column))
                cells.append(f"<td>{value}</td>")
            rows.append("<tr>" + "".join(cells) + "</tr>")
    flush()
    version = re.search(r"^version:\s*(\S+)", (REPO / "metadata.yaml").read_text("utf-8"), re.M)[1]
    return '''<!doctype html><meta charset="utf-8"><style>
    *{box-sizing:border-box}body{margin:0;padding:42px;width:1600px;background:#18222c;color:#ecf0f3;font:21px "Microsoft YaHei","Noto Sans CJK SC",sans-serif}
    header{border-bottom:4px solid #f2da61;margin-bottom:25px;padding-bottom:20px}h1{font-size:54px;margin:0}header p{color:#c6cfd7;margin:12px 0 0}
    main{columns:2;column-gap:25px}section{break-inside:avoid;background:#25313d;padding:20px;margin:0 0 24px;border-top:3px solid #f2da61}
    h2{font-size:28px;color:#f2da61;margin:0 0 12px}table{border-collapse:collapse;width:100%;font-size:18px;line-height:1.65}
    td{border-top:1px solid #43505d;padding:8px 5px;vertical-align:top}td:first-child{width:53%}code{color:#f5e49a;overflow-wrap:anywhere}footer{margin-top:24px;font-size:20px;color:#c6cfd7;line-height:1.8}
    </style><header><h1>Priestess Bot · ''' + escape(version) + '''</h1><p>明日方舟 × 终末地 · AstrBot / NapCat 指令速查</p></header><main>''' + "".join(blocks) + '''</main><footer>
    00:15 双游戏签到；启动补签不发群汇总。活动通知须分别配置两款游戏的发送群，默认留空。<br>
    管理命令需 AstrBot 管理员权限。完整配置与适配差异见仓库 README。<br>
    原项目：Billtranscend/Priestess-bot · Wuthering-17 仅负责 AstrBot 接口适配与维护 · 第三方许可见仓库声明。
    </footer>'''


async def build():
    sys.path.insert(0, str(REPO))
    from browser import launch_browser
    target = REPO / "resources/help/help.webp"
    async with async_playwright() as p:
        browser = await launch_browser(p)
        try:
            page = await browser.new_page(viewport={"width": 1600, "height": 1000}, device_scale_factor=1)
            await page.set_content(help_html(), wait_until="load")
            await page.evaluate("document.fonts.ready")
            image = await page.screenshot(full_page=True)
        finally:
            await browser.close()
    with Image.open(BytesIO(image)) as im:
        im.save(target, "WEBP", quality=85, method=6)
        print(f"Built {target.name}: {im.width} x {im.height}, {target.stat().st_size} bytes")


if __name__ == "__main__":
    asyncio.run(build())
