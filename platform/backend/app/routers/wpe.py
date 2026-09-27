"""WPE 封包编辑域。

**语义不重复实现。** 每个端点只把参数整理成适配层 `/wpe/*` 的请求，
走和网页端同一条链路。另写一套逻辑 → 平台看到的结果和界面看到的迟早不一致。

对应关系（我方 → 适配层）：

    GET    /stat                  → GET  /wpe/stat
    GET    /packets               → GET  /wpe/list
    GET    /packets/{id}          → GET  /wpe/get          （同时返回 packet 与 struct）
    POST   /packets/{id}/replay   → POST /wpe/replay
    POST   /send                  → POST /wpe/send
    GET|PUT /rewrite              → GET|POST /wpe/rewrite
    GET|PUT /hold                 → GET|POST /wpe/hold
    POST   /hold/{id}/release     → POST /wpe/release
    GET    /export                → GET  /wpe/export        （内存分页版）
    GET|POST /exports             → GET  /wpe/export/list · POST /wpe/export/save
    GET    /exports/{name}/download → GET /wpe/export/download
    POST   /import                → POST /wpe/import
    DELETE /packets               → GET  /wpe/clear
"""
from __future__ import annotations

import json
import os
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from ..config import settings
from ..db import get_db
from ..deps import current_user
from ..schemas import (
    HoldSetIn, ImportIn, ReleaseIn, ReplayIn, RewriteSetIn, SendIn,
)
from ..security import decode_token
from ..upstream import adapter_creds, ccpx, local_client

router = APIRouter()


def _q(pairs: dict) -> str:
    """拼 query。None / 空串跳过，布尔转 1/0。"""
    parts = []
    for k, v in pairs.items():
        if v is None or v == '':
            continue
        if isinstance(v, bool):
            v = 1 if v else 0
        parts.append('%s=%s' % (k, quote(str(v), safe='')))
    return ('?' + '&'.join(parts)) if parts else ''


async def _fwd(method: str, path: str, body: bytes = b'',
               ctype: str | None = None) -> dict:
    """转发并按适配层的 {ok,...} 结构原样回传。

    不在这里把上游的错误「修好」—— 上游说什么就报什么。
    """
    code, raw = await ccpx.request(method, path, body, ctype)
    text = raw.decode('utf-8', 'replace')
    if code != 200:
        raise HTTPException(502, detail='适配层返回 %d：%s' % (code, text[:300]))
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise HTTPException(502, detail='适配层返回体不是 JSON：%s' % exc) from exc
    return data if isinstance(data, dict) else {'ok': True, 'data': data}


# --------------------------------------------------------------------------- #
# 读
# --------------------------------------------------------------------------- #
@router.get('/stat')
async def stat(_user=Depends(current_user)):
    return await _fwd('GET', '/wpe/stat')


@router.get('/packets')
async def list_packets(
    since: int = Query(0, ge=0),
    before: int = Query(0, ge=0),
    limit: int = Query(2000, ge=1, le=2000),
    dir: str = '',
    proto: str = '',
    target: str = '',
    hex: str = '',
    label: str = '',
    app: str = '',
    min_len: int = Query(0, ge=0),
    max_len: int = Query(0, ge=0),
    only_hold: bool = False,
    _user=Depends(current_user),
):
    """封包列表。

    `before` 是「加载更早」的唯一入口：只看 id 严格小于 before 的包。
    只有 `since` 的话，后 2000 条永远没有入口 —— 这正是旧版「列表显示不全」的根因。
    """
    return await _fwd('GET', '/wpe/list' + _q({
        'since': since, 'before': before, 'limit': limit, 'dir': dir, 'proto': proto,
        'target': target, 'hex': hex, 'label': label, 'app': app,
        'min_len': min_len, 'max_len': max_len, 'only_hold': only_hold,
    }))


@router.get('/packets/{pid}')
async def get_packet(pid: int, _user=Depends(current_user)):
    """单个封包的完整 hex + 分层结构。

    适配层在同一个响应里给 `packet` 与 `struct`，所以不需要单独的 struct 端点。
    """
    return await _fwd('GET', '/wpe/get' + _q({'id': pid}))


# --------------------------------------------------------------------------- #
# 写
# --------------------------------------------------------------------------- #
@router.post('/packets/{pid}/replay')
async def replay(pid: int, body: ReplayIn, _user=Depends(current_user)):
    if body.id != pid:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail='路径 id 与请求体 id 不一致')
    return await _fwd('POST', '/wpe/replay' + _q({
        'id': pid, 'data': body.data, 'target': body.target,
        'proto': body.proto, 'times': body.times,
    }))


@router.post('/send')
async def send(body: SendIn, _user=Depends(current_user)):
    return await _fwd('POST', '/wpe/send' + _q({
        'proto': body.proto, 'target': body.target, 'data': body.data,
    }))


@router.get('/rewrite')
async def get_rewrite(_user=Depends(current_user)):
    return await _fwd('GET', '/wpe/rewrite')


@router.put('/rewrite')
async def set_rewrite(body: RewriteSetIn, _user=Depends(current_user)):
    """挂/摘自动改写规则。

    规则至少要带一个条件 —— 无条件改写会改掉每一个包，适配层也会拒收。
    这里在入参模型里就挡住，报清楚原因，而不是让下游回一句「规则无效」。
    """
    payload = json.dumps(
        {'rules': [r.model_dump(exclude_none=True) for r in body.rules]},
        ensure_ascii=False).encode('utf-8')
    return await _fwd('POST', '/wpe/rewrite' + _q({'on': body.on}),
                      payload, 'application/json')


@router.get('/hold')
async def get_hold(_user=Depends(current_user)):
    """只读。

    适配层专门为这个端点修过「GET 不该改状态」—— 以前任何一次 GET
    （探测、刷新、误点）都会被当成 on=false 把断点关掉。这里保持只读。
    """
    return await _fwd('GET', '/wpe/hold')


@router.put('/hold')
async def set_hold(body: HoldSetIn, _user=Depends(current_user)):
    """开/关断点。空规则 = 拦下全部（断点语义，与改写不同，是有意的）。"""
    payload = json.dumps(
        {'rule': body.rule.model_dump(exclude_none=True)},
        ensure_ascii=False).encode('utf-8')
    return await _fwd('POST', '/wpe/hold' + _q({'on': body.on}),
                      payload, 'application/json')


@router.post('/hold/{pid}/release')
async def release(pid: int, body: ReleaseIn, _user=Depends(current_user)):
    if body.id != pid:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail='路径 id 与请求体 id 不一致')
    return await _fwd('POST', '/wpe/release' + _q({
        'id': pid, 'action': body.action, 'data': body.data,
    }))


@router.delete('/packets')
async def clear_packets(_user=Depends(current_user)):
    """清空缓冲。

    适配层这个动作挂在 GET /wpe/clear 上。这里用 DELETE 表达语义，
    但**不能**把上游也改成 POST —— 那是另一个服务，改动要单独评估。
    """
    return await _fwd('GET', '/wpe/clear')


# --------------------------------------------------------------------------- #
# 导出 / 导入
# --------------------------------------------------------------------------- #
@router.get('/export')
async def export_json(since: int = Query(0, ge=0),
                      limit: int | None = Query(None, ge=1),
                      max_hex: int | None = Query(None, ge=1),
                      _user=Depends(current_user)):
    """内存分页导出完整 hex。

    带 next_since：没导完时拿它当 since 再请求一次就能续上。
    """
    return await _fwd('GET', '/wpe/export' + _q({
        'since': since, 'limit': limit, 'max_hex': max_hex,
    }))


@router.get('/exports')
async def list_exports(_user=Depends(current_user)):
    return await _fwd('GET', '/wpe/export/list')


@router.post('/exports', status_code=status.HTTP_201_CREATED)
async def save_export(since: int = Query(0, ge=0),
                      kind: str = Query('json', pattern='^(json|txt)$'),
                      _user=Depends(current_user)):
    """落盘导出 —— 不受内存响应上限约束，能拿到整个缓冲。

    这是「导出完整 hex」的正路。刻意做成 POST：它会在磁盘上产生文件，
    是个有副作用的动作（浏览器预取、爬虫扫到都会触发）。
    """
    return await _fwd('POST', '/wpe/export/save' + _q({'since': since, 'kind': kind}))


def _download_token_ok(request: Request, name: str) -> bool:
    """校验 MCP 签发的一次性下载令牌。

    判定三件事，缺一不可：
      · 令牌解得开（同一个 jwt_secret、没过期）
      · scope 是 wpe:download —— 登录令牌不能拿来当下载令牌用
      · 令牌里记的文件名与本次请求的文件名**一致**

    第三条是关键：不然拿到任意一个下载地址，就能把 name 换成别的导出文件。
    令牌里绑死文件名，换一个就失效。

    任何一步不成立都返回 False（不抛异常）—— 调用方接着去查登录态，
    两种凭据是「或」的关系，不是「与」。
    """
    tok = request.query_params.get('t') or ''
    if not tok:
        return False
    payload = decode_token(tok)
    if not payload:
        return False
    if payload.get('scope') != 'wpe:download':
        return False
    return str(payload.get('name') or '') == name


@router.get('/exports/{name}/download')
async def download_export(name: str, request: Request, db=Depends(get_db)):
    """下载落盘文件。

    鉴权两条路，任一满足即可：

    ① 会话 cookie（人在浏览器里点「下载」走这条）；
    ② `?t=<令牌>`（MCP 给的地址走这条）。

    ★ 为什么必须有 ②：`/mcp` 本身**不鉴权**（MCP 客户端没法先登录），
      但它需要把大文件的下载地址交出去。如果那个地址只认 cookie，
      客户端照着取就是 401 —— 等于「给了地址但门锁着」，大文件通道
      整个是死的。令牌与登录同密钥签发，带 scope + 文件名 + 30 分钟 TTL，
      文件名对不上也拒，所以它**不能拿去下别的文件**。

    ★ `name` 只允许纯文件名 —— 不校验的话 ../../etc/passwd 就是任意文件读取。
    """
    if not name or os.path.basename(name) != name or name.startswith('.'):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail='name 必须是纯文件名，不能带路径')

    if not _download_token_ok(request, name):
        # 令牌不成立才去要登录态 —— 顺序反过来的话，带令牌的请求
        # 也会先撞 401，令牌就白签了。
        current_user(request, db)

    url = '%s/wpe/export/download%s' % (
        settings.ccpx_admin_base.rstrip('/'), _q({'name': name}))
    import base64

    # 凭据现读，不读环境变量 —— 与 CcpxAdmin 同一套来源。
    _user, _pwd, _cerr = adapter_creds()
    token = base64.b64encode(('%s:%s' % (_user, _pwd)).encode()).decode()

    client = local_client()
    try:
        req = client.build_request('GET', url, headers={'Authorization': 'Basic ' + token})
        resp = await client.send(req, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(502, detail='拉取上游失败：%s' % exc) from exc

    if resp.status_code != 200:
        body = (await resp.aread())[:300].decode('utf-8', 'replace')
        await resp.aclose()
        await client.aclose()
        raise HTTPException(502, detail='适配层返回 %d：%s' % (resp.status_code, body))

    ctype = ('text/plain; charset=utf-8' if name.endswith('.txt')
             else 'application/json; charset=utf-8')

    async def _stream():
        try:
            async for chunk in resp.aiter_bytes(64 * 1024):
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(
        _stream(),
        media_type=ctype,
        headers={'Content-Disposition': 'attachment; filename="%s"' % name,
                 'Cache-Control': 'no-store'},
    )


@router.post('/import')
async def import_packets(body: ImportIn, _user=Depends(current_user)):
    payload = json.dumps({'packets': body.packets}, ensure_ascii=False).encode('utf-8')
    return await _fwd('POST', '/wpe/import', payload, 'application/json')
