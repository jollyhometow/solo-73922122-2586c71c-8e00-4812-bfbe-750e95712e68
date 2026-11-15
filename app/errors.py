"""取数 API 的错误类型。

每种错误映射到明确的 HTTP 状态码（见 main.py），
保证"明确失败，不提供半成品"：任何异常都会中断取数，绝不返回部分数据。
"""


class ClipError(Exception):
    """取数失败的基类。"""

    http_status = 500
    error_code = "internal_error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class SourceError(ClipError):
    """源服务器问题：不可达、不支持 Range、缺少强 ETag、响应异常等。"""

    http_status = 502
    error_code = "source_error"


class VersionChangedError(ClipError):
    """读取过程中源影像换版（ETag 变化 / If-Range 失效 / 长度变化）。"""

    http_status = 409
    error_code = "version_changed"


class UnsupportedFormatError(ClipError):
    """源影像不满足约束（非无压缩/非双波段/非分块/非 uint16/缺地理参考）。"""

    http_status = 415
    error_code = "unsupported_format"


class WindowError(ClipError):
    """窗口参数越界（超出影像范围）。"""

    http_status = 422
    error_code = "window_out_of_bounds"
