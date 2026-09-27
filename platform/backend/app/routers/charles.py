"""Charles 配置域。

路由只做参数整理与线程池调度；所有竞态相关的逻辑都在 `charles_store`，
不在这一层重新发明。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from .. import charles_store as cs
from ..deps import current_user
from ..upstream import charles as charles_webif

router = APIRouter()


class ChangeIn(BaseModel):
    path: str = Field(min_length=1)
    value: str = ""
    type: str = Field("str", pattern="^(str|bool|int)$")


class ChangesIn(BaseModel):
    changes: list[ChangeIn] = Field(min_length=1)
    restart: bool = True


class RawIn(BaseModel):
    xml: str = Field(min_length=1)
    restart: bool = True


class ServiceIn(BaseModel):
    action: str = Field(pattern="^(start|stop|restart)$")


class BackupIn(BaseModel):
    tag: str = Field("manual", max_length=32)


class RestoreIn(BaseModel):
    restart: bool = True


class LicenseIn(BaseModel):
    # 授权码是 Charles 自己生成的一串（当前部署是 16 位十六进制），
    # 但别把长度卡死 —— 换版本可能变，卡死等于给未来埋一个 422。
    name: str = Field(min_length=1, max_length=120)
    key: str = Field(min_length=8, max_length=120)
    restart: bool = True


@router.get('/state')
async def get_state(_user=Depends(current_user)):
    return {'ok': True, 'data': await run_in_threadpool(cs.state)}


@router.get('/config')
async def get_config(_user=Depends(current_user)):
    try:
        xml = await run_in_threadpool(cs.read_cfg)
    except OSError as exc:
        raise HTTPException(500, detail='读取配置失败：%s' % exc) from exc
    return {'ok': True, 'xml': xml, 'size': len(xml.encode('utf-8'))}


@router.post('/changes')
async def post_changes(body: ChangesIn, _user=Depends(current_user)):
    """增量改动。

    只改指定路径，未提到的节点保持原样。逐条报错（哪条没找到、哪条不是整数），
    不做「整批静默跳过」—— 静默忽略比报错坏得多。
    """
    res = await run_in_threadpool(
        cs.apply_changes,
        [c.model_dump() for c in body.changes],
        body.restart,
    )
    if not res.get('ok'):
        raise HTTPException(400, detail=res.get('error') or '应用失败')
    return res


@router.post('/raw')
async def post_raw(body: RawIn, _user=Depends(current_user)):
    """整份 XML 覆盖写入。会校验结构、自动补齐 charles 处理指令。"""
    res = await run_in_threadpool(cs.apply_raw, body.xml, body.restart)
    if not res.get('ok'):
        raise HTTPException(400, detail=res.get('error') or '写入失败')
    return res


@router.get('/backups')
async def get_backups(_user=Depends(current_user)):
    return {'ok': True, 'data': await run_in_threadpool(cs.list_backups)}


@router.post('/backups', status_code=status.HTTP_201_CREATED)
async def create_backup(body: BackupIn, _user=Depends(current_user)):
    try:
        name = await run_in_threadpool(cs.backup, body.tag)
    except OSError as exc:
        raise HTTPException(500, detail='备份失败：%s' % exc) from exc
    return {'ok': True, 'name': name}


@router.post('/backups/{name}/restore')
async def restore_backup(name: str, body: RestoreIn, _user=Depends(current_user)):
    res = await run_in_threadpool(cs.restore, name, body.restart)
    if not res.get('ok'):
        raise HTTPException(400, detail=res.get('error') or '恢复失败')
    return res


@router.get('/logs')
async def get_logs(lines: int = Query(120, ge=1, le=5000),
                   _user=Depends(current_user)):
    return {'ok': True, 'data': await run_in_threadpool(cs.logs, lines)}


@router.post('/service')
async def control_service(body: ServiceIn, _user=Depends(current_user)):
    """服务控制。

    适配层与 Charles 是两条独立链路，这里只管 Charles。适配层的启停
    走 `/api/v1/ccpx/*`。
    """
    rc, out = await run_in_threadpool(cs.svc, body.action)
    return {'ok': rc == 0, 'action': body.action, 'rc': rc,
            'output': out.strip(), 'state': await run_in_threadpool(cs.svc_active)}


@router.get('/ca')
async def get_ca(_user=Depends(current_user)):
    return {'ok': True, 'data': await run_in_threadpool(cs.ca_info)}


@router.get('/license')
async def get_license(_user=Depends(current_user)):
    """Charles 授权（注册）状态。

    ★ headless 下没有「Help → Register」面板，这是唯一能看见授权到底
      有没有生效的地方。之前平台完全没这个概念 —— 代理能不能长期干活
      全押在一个看不见的试用期上，这是运维盲区，不是小事。
    """
    return {'ok': True, 'data': await run_in_threadpool(cs.license_info)}


@router.post('/license')
async def set_license(body: LicenseIn, _user=Depends(current_user)):
    """写入授权（name + key）。走停-改-起通道，理由同 `/raw`。"""
    res = await run_in_threadpool(cs.apply_license, body.name, body.key, body.restart)
    if not res.get('ok'):
        raise HTTPException(400, detail=res.get('error') or '写入授权失败')
    return res


@router.get('/webif')
async def webif(path: str = Query('/', description='Charles Web Interface 子路径'),
                _user=Depends(current_user)):
    """Charles Web Interface 反代。

    必须发绝对 URI 请求行，否则回 503 —— 它挂在代理端口上，按代理语义解析。
    这一层负责把非 HTML 的 Content-Type 原样带出去。
    """
    code, headers, body = await run_in_threadpool(charles_webif.get, path)

    ctype = 'text/html; charset=utf-8'
    for k, v in (headers or {}).items():
        if k.lower() == 'content-type':
            ctype = v
            break
    return Response(content=body, status_code=code, media_type=ctype)


@router.get('/help')
async def get_help(name: str = Query('', description='帮助页名；留空则回名单'),
                   _user=Depends(current_user)):
    """charles.jar 自带的官方帮助页。

    旧栈是 `GET /api/help?name=X`，前端把返回的 HTML 塞进 iframe 的 srcdoc。
    这里保持同样的用法，只把路径归到 charles 域下（帮助内容本来就是 Charles 的）。

    留空 name 时回**名单** —— 排障时想知道「到底有哪些页」，
    否则只能一个个试名字，试错成本全在用户身上。
    """
    if not name:
        return {'ok': True, 'data': await run_in_threadpool(cs.help_names)}
    html = await run_in_threadpool(cs.help_html, name)
    if html is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail='未找到帮助页: %s' % name)
    return Response(content=html, media_type='text/html; charset=utf-8')
