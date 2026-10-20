"""最小化配置模块，兼容原插件 filters.py 的路径依赖"""

from pathlib import Path
from .storage import get_data_dir

# 插件数据目录（AstrBot 规范：data/plugin_data/{plugin_name}/）
DATA_DIR = get_data_dir()

# 缓存目录（下载的游戏资源、临时文件）
CACHE_DIR = DATA_DIR / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# 资源目录（模板、字体、静态图片）
RES_DIR = Path(__file__).parent / "resources"
TEMPLATES_DIR = RES_DIR / "templates"

# 游戏资源下载路由
RESOURCE_ROUTES = ["portrait", "skill", "avatar"]
DATA_ROUTES = ["gamedata/excel/gacha_table.json", "gamedata/excel/character_table.json"]
GACHA_DATA_PATH = DATA_DIR / "gamedata" / "excel"


class _SimpleConfig:
    """兼容层：提供与原插件 config 对象相同的属性访问

    AstrBot 使用 _conf_schema.json 管理配置，但原插件代码依赖 config.xxx。
    这里提供默认值，实际值从 AstrBot 上下文获取（如需要）。
    """

    github_proxy_url: str = ""
    github_token: str = ""
    check_res_update: bool = False
    background_source: str = "default"
    endfield_background_simple: bool = False
    rogue_background_source: str = "rogue"
    argot_expire: int = 300
    gacha_render_max: int = 30
    ef_gacha_render_max: int = 5


config = _SimpleConfig()
