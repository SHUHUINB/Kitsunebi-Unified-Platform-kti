"""应用入口。

    uvicorn app.main:app --host 127.0.0.1 --port 8900 --reload

路由挂载一览（与旧栈的对照见 docs/API.md）：

    /api/v1/auth     认证
    /api/v1/overview 概览聚合
    /api/v1/charles  Charles 配置域
    /api/v1/ccpx     适配层域
    /api/v1/wpe      封包编辑域
    /api/v1/kami     卡密域（取代一花 PHP）
    /mcp             MCP 端点（路径与旧栈一致，客户端无需改动）
"""
from __future__ import annotations

import difflib
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (FileResponse, JSONResponse, RedirectResponse,
                               Response)
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import settings
from .db import init_db
from .upstream import UpstreamError

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)-7s %(name)s | %(message)s',
)
log = logging.getLogger('rewrite')


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    if not settings.jwt_secret:
        log.warning('REWRITE_JWT_SECRET 未设置 —— 正在使用进程级临时密钥，重启后所有会话失效')
    if settings.allow_anon:
        log.warning('REWRITE_ALLOW_ANON 已开启 —— 匿名可访问，仅限内网调试')
    # ★ 启动自检：Charles 配置路径必须存在。
    #   这条踩过一次真坑：默认值曾被写成 `~/.charles/config.xml`（不存在），
    #   而系统**不报错**—— `/charles/state` 老老实实回 `configSize: 0`，
    #   配置页读空、写入写进没人看的文件，Charles 毫无反应。
    #   所有冒烟都是绿的，因为没有一条断言在问「这个文件到底存不存在」。
    #   把判据放到启动日志里，是最省事的一次性防线。
    # ★ 自愈纠正必须先报出来。`_resolve_charles_config()` 会在 env 里是坏值、
    #   而规范路径存在时改用规范路径 —— 那属于「我替你改了一个设置」，
    #   必须让人看见，否则用户永远不知道 rewrite.env 里躺着一个错的路径，
    #   下次换台机器又踩一遍。
    if settings.charles_config_note:
        log.warning('Charles 配置路径已纠正：%s', settings.charles_config_note)
    if not settings.charles_config.exists():
        log.warning('Charles 配置不存在：%s —— 配置页会读到空、「增量改动」将无效果。'
                    '真文件通常是 ~/.charles.config（单文件），不是 ~/.charles/config.xml',
                    settings.charles_config)
    if not settings.charles_ca_cer.exists():
        log.warning('Charles 根证书未找到：%s —— /ca 与设备侧证书下载会 404',
                    settings.charles_ca_cer)
    log.info('%s %s 就绪', settings.app_name, __version__)
    yield


app = FastAPI(
    title=settings.site_name + ' API',
    version=__version__,
    description='Charles / CCProxy / WPE / 卡密 —— kit端口统一平台-soun 的统一后端。',
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=True,          # 前端要带 Cookie
    allow_methods=['*'],
    allow_headers=['*'],
)


@app.middleware('http')
async def _normalize_mcp_slash(request: Request, call_next):
    """`/mcp/` 与 `/mcp` 等价 —— 带尾斜杠的 MCP 客户端也要能连上。

    ★ 为什么不能指望 Starlette 自带的 `redirect_slashes`：那条逻辑只在
      **完全没有路由匹配**时才生效。而下面的 SPA 通配 `/{full_path:path}`
      会先把 `/mcp/` 匹配掉，请求于是落进兜底 —— POST 回 405，
      客户端那边表现成「连不上 MCP」，跟路由注册看不出关系。

    ★ 为什么在 scope 里改 path 而不是回 307：307 要客户端自己再发一次，
      而 POST 的 307 并非所有 HTTP 客户端都会跟着走（MCP 客户端尤其如此）。
      改 path 是路由前的一次改写，body 原样保留，客户端无感。
    """
    if request.scope.get('path') == '/mcp/':
        request.scope['path'] = '/mcp'
    return await call_next(request)


@app.exception_handler(UpstreamError)
async def _upstream_error(_req: Request, exc: UpstreamError):
    """上游失败如实回 502，不吞成 200。

    旧栈有过多起「适配层侧全绿、用户侧永远下不来」的案例，根因都是中间层
    把正常结果误判成错误（或反过来）。上游说什么就报什么。
    """
    return JSONResponse(
        status_code=502,
        content={'ok': False, 'error': '上游返回 %d：%s' % (exc.status, exc.detail)},
    )


@app.get('/healthz', tags=['meta'])
async def healthz():
    return {'ok': True, 'app': settings.app_name, 'version': __version__}


from .routers import (auth, ccpx, charles, device, kami, mcp,  # noqa: E402
                      overview, wpe)

API = '/api/v1'
app.include_router(auth.router,     prefix=API + '/auth',     tags=['auth'])
app.include_router(overview.router, prefix=API + '/overview', tags=['overview'])
app.include_router(charles.router,  prefix=API + '/charles',  tags=['charles'])
app.include_router(ccpx.router,     prefix=API + '/ccpx',     tags=['ccpx'])
app.include_router(wpe.router,      prefix=API + '/wpe',      tags=['wpe'])
app.include_router(kami.router,     prefix=API + '/kami',     tags=['kami'])
app.include_router(mcp.router,      prefix='',                tags=['mcp'])
# 设备侧无登录入口（/ok /socks /ruleset* /ca.*）。
# ★ 必须在这里挂 —— 下面的 SPA 通配 `/{full_path:path}` 会把未注册的 GET
#   路径一律兜成 index.html（200，看起来「正常」），而客户端拿到的是 HTML
#   不是证书。路由按注册顺序匹配，注册在通配之前才能命中。
app.include_router(device.router,   prefix='',                tags=['device'])


# --------------------------------------------------------------------------- #
# 路由对账表 —— 兜底路由要能分清「没这条路由」和「方法不对」。
#
# ★ 不能用 `app.routes` 手遍历。这个 FastAPI 版本里 `include_router` **不会**
#   把子路由摊平进 `app.routes`，而是塞一个 `_IncludedRouter` 占位对象
#   （没有 `path` 属性）。照着 `app.routes` 建表只会数到默认的 4 条文档路由
#   加 `/healthz` —— 表是残的，于是兜底换个姿势继续撒谎。
#   实测踩过：`len(app.routes)=17` 而 `len(_ROUTES)=5`，`did_you_mean` 恒空。
#
# ★ 权威来源用 `app.openapi()`：`paths` 是公开契约，换 FastAPI 版本也稳。
#   递归私有类名（`_IncludedRouter`）是反过来的做法 —— 一改名就静默退化。
#
# ★ 懒加载 + 缓存：openapi() 要遍历全部路由，放在 import 时白付一次开销。
#   而且它必须在全部 include_router 之后才准 —— 懒加载天然满足这个前提。
# --------------------------------------------------------------------------- #
_SKIP_METHODS = {'HEAD', 'OPTIONS'}
_PARAM_SEG = re.compile(r'^\{[^}]+\}$')
_ROUTE_CACHE = None


def _path_regex(template: str):
    """`/api/v1/kami/{kid}` → 能匹配真实 URL 的正则（参数段吃一个路径段）。

    ★ 不能拿模板直接做等值比较：真实请求是 `/api/v1/kami/123`，
      和模板 `/api/v1/kami/{kid}` 永远不相等，于是「方法不对」永远判不出来。
    """
    segs = ['[^/]+' if _PARAM_SEG.match(s) else re.escape(s)
            for s in template.split('/')]
    return re.compile('^' + '/'.join(segs) + '$')


def _route_table():
    """[(正则, 模板, 方法集)] —— 懒加载 + 缓存。"""
    global _ROUTE_CACHE
    if _ROUTE_CACHE is None:
        rows = []
        for tpl, ops in (app.openapi().get('paths') or {}).items():
            ms = {m.upper() for m in ops if m.upper() not in _SKIP_METHODS}
            if ms:
                rows.append((_path_regex(tpl), tpl, ms))
        _ROUTE_CACHE = rows
    return _ROUTE_CACHE


def _match_route(url: str):
    """→ (模板, 方法集)；没匹配到就是 (None, None)。"""
    for rx, tpl, ms in _route_table():
        if rx.match(url):
            return tpl, ms
    return None, None


def _route_templates():
    return sorted({tpl for _, tpl, _ in _route_table()})


# --------------------------------------------------------------------------- #
# 生产托管：把 frontend/dist 直接挂上来。
#
# 前后端分离在开发期靠 vite 的 proxy；上线时如果还要单开一个静态服务，
# 等于多一个要管的进程和一份 CORS 配置。这里直接挂，SPA 回退也一起处理。
# --------------------------------------------------------------------------- #
_DIST = Path(__file__).resolve().parents[2] / 'frontend' / 'dist'


@app.get('/_frontend', include_in_schema=False, tags=['meta'])
async def _frontend_status():
    """构建产物在不在 —— 不在就说明还没跑 `npm run build`。

    ★ 必须注册在下面的 SPA 通配路由**之前**，否则会被通配吃掉，
      永远回一份 index.html（看起来「正常」，实际什么也没说）。
    """
    return {'ok': _DIST.is_dir(), 'dist': str(_DIST),
            'index': (_DIST / 'index.html').is_file(),
            'hint': None if _DIST.is_dir() else '尚未构建：cd frontend && npm install && npm run build'}


if (_DIST / 'assets').is_dir():
    app.mount('/assets', StaticFiles(directory=str(_DIST / 'assets')), name='assets')

    @app.get('/favicon.ico', include_in_schema=False)
    async def _favicon():
        return Response(status_code=204)

    @app.get('/{full_path:path}', include_in_schema=False)
    async def _spa(full_path: str):
        """SPA 回退。

        只兜底 GET，且**只兜非 API 路径** —— 把 /api/v1/xxx 拼错的请求
        回一份 index.html（200）会让排查变成噩梦，所以 API 前缀不走回退。

        ★ API 前缀这里要**分清三种情况**，不能一律回「没有这条路由」：

          ① 只差一个尾斜杠（`/api/v1/kami/`）→ 307 去正确地址；
          ② 路径**存在**但方法不对（`GET /api/v1/kami` 只收 POST）→ 405，
             并把允许的方法列出来。回 404 说「没有这条路由」是在撒谎，
             会让人去翻路由注册代码找一个明明存在的端点；
          ③ 真没有 → 404，附最接近的候选，拼错一眼可见。
        """
        url = '/' + full_path
        if url.startswith(('/api/', '/mcp')):
            stripped = url.rstrip('/') or '/'
            if stripped != url and _match_route(stripped)[0]:
                return RedirectResponse(stripped, status_code=307)
            tpl, ms = _match_route(url)
            if tpl:
                allow = sorted(ms)
                raise HTTPException(
                    405,
                    detail='%s 只接受 %s，当前是 GET' % (tpl, '/'.join(allow)),
                    headers={'Allow': ', '.join(allow)},
                )
            raise HTTPException(404, detail={
                'error': 'No such API route',
                'path': url,
                'did_you_mean': difflib.get_close_matches(
                    url, _route_templates(), n=3, cutoff=0.6) or None,
                'routes': '%d 条已注册路由见 /openapi.json' % len(_route_templates()),
            })
        candidate = _DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(str(candidate))
        return FileResponse(str(_DIST / 'index.html'))
