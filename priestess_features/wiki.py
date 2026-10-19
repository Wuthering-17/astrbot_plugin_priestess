"""AstrBot service for the AKEData Endfield wiki cards."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .endfield_wiki import changes, data, render
from .endfield_wiki.lookup import Resolver
from .endfield_guide.echoes import partners as team_partners
from ..render_adapter import _try_star_html_render

logger = logging.getLogger("astrbot")


class EndfieldWiki:
    """Sync AKEData tables and render searchable operator, weapon and equipment cards."""

    sync_interval = 600

    def __init__(self, plugin, data_dir: Path) -> None:
        self.plugin = plugin
        self.data_dir = (data_dir / "endfield_wiki").resolve()
        self.index_file = self.data_dir / "index.json"
        self.state_file = self.data_dir / "state.json"
        self.image_dir = self.data_dir / "img"
        self.misses_file = self.data_dir / "misses.json"
        self.aliases_file = Path(__file__).parent / "endfield_wiki" / "aliases.json"
        self._sync_lock = asyncio.Lock()
        self._resolver_key: tuple[float, float] | None = None
        self._resolver: Resolver | None = None
        self._index: dict | None = None

    @staticmethod
    def _read_json(path: Path, default):
        return json.loads(path.read_text("utf-8")) if path.exists() else default

    @staticmethod
    def _write_json(path: Path, value) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False), "utf-8")
        tmp.replace(path)

    def resolver(self) -> tuple[dict, Resolver] | None:
        if not self.index_file.exists():
            return None
        key = (self.index_file.stat().st_mtime, self.aliases_file.stat().st_mtime)
        if key != self._resolver_key:
            index = self._read_json(self.index_file, {})
            self._index = index
            self._resolver = Resolver(index, self._read_json(self.aliases_file, {}))
            self._resolver_key = key
        return self._index, self._resolver

    def record_miss(self, query: str) -> None:
        if not re.fullmatch(r"[\u4e00-\u9fffA-Za-z0-9.·]{2,10}", query):
            return
        try:
            misses = self._read_json(self.misses_file, {})
            misses[query] = misses.get(query, 0) + 1
            self._write_json(self.misses_file, misses)
        except Exception:
            logger.debug("[Priestess] Could not record an unresolved wiki query", exc_info=True)

    async def ensure_ready(self) -> None:
        if not self.index_file.exists():
            await self.sync()

    async def sync(self, force: bool = False) -> str:
        async with self._sync_lock:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            state = self._read_json(self.state_file, {})
            async with data.http_client() as client:
                current_index = self._read_json(self.index_file, {})
                outdated = current_index.get("schema", 1) < data.INDEX_SCHEMA
                manifest = await data.fetch_manifest(client)
                latest = next((item for item in manifest.get("versions", []) if item.get("id") == manifest.get("latest")), None)
                if not latest:
                    raise RuntimeError("AKEData manifest does not contain its latest version")
                previous = state.get("version") or current_index.get("version")
                if latest["id"] == previous and self.index_file.exists() and not force and not outdated:
                    return f"已是最新版本 {previous}"

                table_key = hashlib.sha256(latest["tableCfgPath"].encode()).hexdigest()[:16]
                table_dir = self.data_dir / "tables_cache" / table_key
                complete = table_dir / ".complete"
                await data.download_tables(client, latest["tableCfgPath"], table_dir, refresh=force and complete.exists())
                counts = await asyncio.to_thread(data.build_index, table_dir, latest, self.index_file)
                complete.touch()

            self._write_json(self.state_file, {"version": latest["id"], "synced_at": time.time()})
            self._resolver_key = None
            logger.info("[Priestess] Endfield wiki index built for %s: %s", latest["id"], counts)
            if previous and previous != latest["id"]:
                updated_index = self._read_json(self.index_file, {})
                added = changes.added(current_index, updated_index)
                lines = changes.notice_lines(added)
                logger.info("[Priestess] Endfield wiki data additions: %s", "；".join(lines))
                await self._notify_groups(
                    "\n".join(
                        [
                            f"AKEData 终末地解包数据已更新\n{previous} → {latest['id']}（{latest.get('publishedAt', '')[:10]}）",
                            *lines,
                        ]
                    )
                )
            return f"已同步 {latest['id']}（干员 {counts['operators']}，武器 {counts['weapons']}）"

    async def _notify_groups(self, message: str) -> None:
        """Send version notices through the configured OneBot v11 adapter."""
        sent = 0
        for platform in self.plugin.context.platform_manager.platform_insts:
            meta = platform.meta()
            bot = getattr(platform, "bot", None)
            if not bot or "aiocqhttp" not in (getattr(meta, "id", "") + getattr(meta, "name", "")).lower():
                continue
            try:
                groups = await bot.call_action("get_group_list")
            except Exception:
                logger.warning("[Priestess] Could not list QQ groups for wiki update notice", exc_info=True)
                continue
            for group in groups or []:
                try:
                    await bot.call_action(
                        "send_group_msg",
                        group_id=int(group["group_id"]),
                        message=message,
                    )
                    sent += 1
                except Exception:
                    logger.warning("[Priestess] Could not send wiki update notice to one QQ group", exc_info=True)
                await asyncio.sleep(3)
        logger.info("[Priestess] Endfield wiki update notice sent to %d QQ groups", sent)

    async def _cached_image(self, url: str, name: str) -> str:
        self.image_dir.mkdir(parents=True, exist_ok=True)
        path = self.image_dir / name
        if not path.exists():
            try:
                async with data.http_client() as client:
                    response = await client.get(url, timeout=20)
                    response.raise_for_status()
                tmp = path.with_suffix(".part")
                tmp.write_bytes(response.content)
                tmp.replace(path)
            except Exception as error:
                logger.warning("[Priestess] Endfield wiki icon unavailable (%s)", type(error).__name__)
                return ""
        return path.as_uri()

    @staticmethod
    def _inline_local_images(html: str) -> str:
        """Inline downloaded icons so AstrBot's remote html_render can access them."""
        def replace(match: re.Match) -> str:
            src = match.group(1)
            if not src.startswith("file://"):
                return match.group(0)
            raw_path = unquote(urlsplit(src).path)
            if re.match(r"^/[A-Za-z]:/", raw_path):
                raw_path = raw_path[1:]
            path = Path(raw_path)
            if not path.is_file():
                return match.group(0)
            mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            return f'src="data:{mime};base64,{encoded}"'

        return re.sub(r'src="([^"]+)"', replace, html)

    async def render_result(self, query: str) -> tuple[str, str]:
        loaded = self.resolver()
        if not loaded:
            return "text", "终末地资料库尚未同步，请稍后再试或联系管理员执行 /资料库更新。"
        index, resolver = loaded
        result = resolver.resolve(query)
        if result is None:
            return "miss", ""
        if result.kind == "candidates":
            return "text", f"找到多个结果：{'、'.join(result.candidates)}\n请输入更完整的名字"
        if result.kind == "no_signature":
            operator = index["operators"][result.id]
            recommended = "、".join(index["weapons"][wid]["name"] for wid in operator.get("recommended", []) if wid in index["weapons"])
            return "text", f"{operator['name']} 没有专属武器" + (f"，推荐武器：{recommended}" if recommended else "")

        version = index.get("version", "")
        guide = getattr(self.plugin, "guide", None)
        if guide:
            optout = guide.echo_store.optout()
            build_stats = guide.build_store.stats(optout)
            min_sample = 3
        else:
            optout = set()
            build_stats = {"operators": {}, "sets": {}}
            min_sample = 3
        if result.kind == "weapon":
            weapon = index["weapons"][result.id]
            icon = await self._cached_image(data.weapon_icon_url(weapon["icon"]), f"wpn_{weapon['icon']}.png")
            html = render.weapon_html(weapon, icon, version, result.note)
        elif result.kind == "equip_set":
            equip_set = index["equip_sets"][result.id]
            pieces = equip_set["pieces"]
            sources = await asyncio.gather(*(self._cached_image(data.equip_icon_url(piece["icon"]), f"eq_{piece['icon']}.png") for piece in pieces))
            icons = {piece["id"]: src for piece, src in zip(pieces, sources)}
            usage = guide.build_store.wearers({piece["name"] for piece in pieces}, optout) if guide and equip_set.get("loose") else build_stats["sets"].get(equip_set["name"])
            html = render.equip_set_html(equip_set, icons, version, usage, min_sample, result.note)
        else:
            operator = index["operators"][result.id]
            icon = await self._cached_image(data.char_icon_url(operator["id"]), f"chr_{operator['id']}.png")
            signature = index["weapons"].get(operator.get("signature_weapon") or "", {}).get("name", "")
            builds = build_stats["operators"].get(operator["name"])
            equip_items = index.get("equip_items", {})
            worn = {piece["name"] for build in (builds or {}).get("builds", []) for piece in build["pieces"]}
            names = [name for name in worn if name in equip_items]
            equip_sources = await asyncio.gather(*(self._cached_image(data.equip_icon_url(equip_items[name]["icon"]), f"eq_{equip_items[name]['icon']}.png") for name in names))
            equip_icons = {name: {"src": src, "attrs": equip_items[name]["attrs"]} for name, src in zip(names, equip_sources)}
            ship_icons = [slot["icon"] for slot in operator.get("logistics", []) if slot.get("icon")]
            ship_sources = await asyncio.gather(*(self._cached_image(data.logistics_icon_url(icon_id), f"ship_{icon_id}.png") for icon_id in ship_icons))
            team = None
            mate_icons = {}
            if guide:
                endmin_ids = {"chr_0003_endminf": "chr_0002_endminm", "chr_9000_endmin": "chr_0002_endminm"}
                clear_data = guide.echo_store.load()
                for qq in optout:
                    clear_data.get("members", {}).pop(qq, None)
                known_ids = list(index["operators"]) + list(endmin_ids) + list(endmin_ids.values())
                by_hash = {hashlib.md5(char_id.encode()).hexdigest(): char_id for char_id in known_ids}
                for member in clear_data.get("members", {}).values():
                    for record in member.get("records", {}).values():
                        for character in record.get("team", []):
                            character["id"] = by_hash.get(character.get("id", ""), character.get("id", ""))
                team = team_partners(clear_data, endmin_ids).get(operator["id"])
                mate_ids = sorted(
                    ({char_id for char_id, _ in (team or {}).get("mates", [])}
                     | {char_id for team_ids, _ in (team or {}).get("teams", []) for char_id in team_ids})
                    - {operator["id"]}
                )
                mate_sources = await asyncio.gather(*(self._cached_image(data.char_icon_url(char_id), f"chr_{char_id}.png") for char_id in mate_ids))
                names_by_id = {char_id: item["name"] for char_id, item in index["operators"].items()}
                mate_icons = {char_id: {"src": src, "name": names_by_id.get(char_id, "")} for char_id, src in zip(mate_ids, mate_sources)}
            html = render.operator_html(
                operator,
                icon,
                version,
                signature,
                builds=builds,
                min_sample=min_sample,
                pieces=equip_icons,
                logistics_icons=dict(zip(ship_icons, ship_sources)),
                team=team,
                mates=mate_icons,
            )

        html = self._inline_local_images(html)
        image_path = await _try_star_html_render(
            self.plugin,
            html,
            width=render.WIDTH,
            base_url=self.data_dir.as_uri(),
        )
        return "image", image_path
