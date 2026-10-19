"""工具函数：Token 自动刷新

原插件使用装饰器模式处理 Token 失效，
AstrBot 版本改为显式调用函数，以便在刷新后更新数据库。
"""

from collections.abc import Callable, Coroutine
from typing import ParamSpec, TypeVar, Concatenate

from astrbot.api import logger

from .api import SklandLoginAPI
from .schemas import CRED
from .exception import LoginException, UnauthorizedException, RequestException

P = ParamSpec("P")
R = TypeVar("R")


async def refresh_access_token(user) -> bool:
    """使用 access_token 刷新 cred 和 cred_token

    Args:
        user: SkUser 对象（含 access_token, cred, cred_token）

    Returns:
        bool: 刷新是否成功
    """
    if not user.access_token:
        logger.warning(f"[Skland] 用户 {user.platform_user_id} 没有 access_token，无法刷新")
        return False

    try:
        grant_code = await SklandLoginAPI.get_grant_code(user.access_token, 0)
        new_cred = await SklandLoginAPI.get_cred(grant_code)
        user.cred = new_cred.cred
        user.cred_token = new_cred.token
        logger.info(f"[Skland] 用户 {user.platform_user_id} access_token 已自动刷新")
        return True
    except (RequestException, LoginException, UnauthorizedException) as e:
        logger.error(f"[Skland] 刷新 access_token 失败: {e}")
        return False


async def refresh_cred_token(user) -> bool:
    """使用 cred 刷新 cred_token

    Args:
        user: SkUser 对象（含 cred, cred_token）

    Returns:
        bool: 刷新是否成功
    """
    try:
        new_token = await SklandLoginAPI.refresh_token(user.cred)
        user.cred_token = new_token
        logger.info(f"[Skland] 用户 {user.platform_user_id} cred_token 已自动刷新")
        return True
    except (RequestException, LoginException, UnauthorizedException) as e:
        logger.error(f"[Skland] 刷新 cred_token 失败: {e}")
        return False


async def call_api_with_refresh(
    user,
    api_func: Callable[Concatenate[CRED, P], Coroutine[None, None, R]],
    cred: CRED,
    *args: P.args,
    **kwargs: P.kwargs,
) -> R:
    """带自动 Token 刷新的 API 调用包装器

    设计为接收一个以 CRED 为第一个参数的 API 函数，
    这样刷新 Token 后可以重新构造 CRED 并重试。

    调用逻辑：
    1. 先尝试调用 api_func(cred, *args, **kwargs)
    2. 如果遇到 LoginException → 刷新 access_token → 用新 cred 重试
    3. 如果遇到 UnauthorizedException → 刷新 cred_token → 用新 cred 重试
    4. 如果刷新后仍然失败 → 重新抛出异常

    Args:
        user: SkUser 对象（用于刷新和保存 token）
        api_func: API 函数，第一个参数必须是 CRED
        cred: 当前的认证凭据
        *args, **kwargs: 传给 api_func 的其余参数

    Returns:
        api_func 的返回值

    Raises:
        LoginException: access_token 刷新失败或无效
        UnauthorizedException: cred_token 刷新失败或无效
        RequestException: 其他请求错误
    """
    try:
        return await api_func(cred, *args, **kwargs)
    except LoginException as e:
        logger.info(f"[Skland] 用户 {user.platform_user_id} access_token 失效，尝试刷新...")
        if await refresh_access_token(user):
            new_cred = CRED(cred=user.cred, token=user.cred_token, userId=cred.userId)
            return await api_func(new_cred, *args, **kwargs)
        else:
            raise LoginException(f"Token 已失效，请使用 /sk login 重新扫码绑定。\n原始错误: {e}") from e
    except UnauthorizedException as e:
        logger.info(f"[Skland] 用户 {user.platform_user_id} cred_token 失效，尝试刷新...")
        if await refresh_cred_token(user):
            new_cred = CRED(cred=user.cred, token=user.cred_token, userId=cred.userId)
            return await api_func(new_cred, *args, **kwargs)
        else:
            raise UnauthorizedException(f"Token 已失效，请使用 /sk login 重新扫码绑定。\n原始错误: {e}") from e
