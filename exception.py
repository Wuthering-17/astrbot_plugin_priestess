"""自定义异常类（脱离 NoneBot 依赖）"""


class SklandException(Exception):
    """异常基类"""


class RequestException(SklandException):
    """请求错误"""


class UnauthorizedException(SklandException):
    """登录授权错误"""


class LoginException(SklandException):
    """登录错误"""
