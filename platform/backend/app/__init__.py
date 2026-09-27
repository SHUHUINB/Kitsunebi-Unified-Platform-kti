"""统一后端 · 应用包。

分层约定（重构后不再有「必须放在 X 之前」这类隐性契约）：

    config   环境与部署参数
    security 密码哈希 / JWT 签发与校验
    db       引擎与会话
    models   持久化模型
    schemas  请求/响应契约
    upstream 对既有服务（适配层 / Charles）的客户端
    deps     依赖注入：鉴权在这里收口，路由只声明「需要登录」
    routers  按业务域拆分：auth / overview / charles / ccpx / wpe / kami / mcp
"""

__version__ = "1.0.0"
