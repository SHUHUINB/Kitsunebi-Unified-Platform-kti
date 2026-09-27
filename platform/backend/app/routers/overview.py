"""概览域。

把三套系统的健康状态聚合成一个端点，取代旧栈「首页要连打五六个接口」的做法。
单个上游挂掉不影响整体返回 —— 每项独立标注状态，而不是整个请求 500。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import charles_store as cs
from ..db import get_db
from ..deps import current_user
from ..models import Account, Kami, KamiApp, KamiServer, KamiUser
from ..upstream import ccpx

router = APIRouter()


@router.get('')
async def overview(_user=Depends(current_user), db: Session = Depends(get_db)):
    out: dict = {'ok': True, 'data': {}}

    # ---- Charles ----
    try:
        out['data']['charles'] = {
            'status': 'up',
            'state': await run_in_threadpool(cs.state),
            'backups': len(await run_in_threadpool(cs.list_backups)),
        }
    except OSError as exc:
        out['data']['charles'] = {'status': 'down', 'error': str(exc)}

    # ---- 适配层 ----
    try:
        status = await ccpx.status()
        try:
            live = await ccpx.live()
        except Exception as exc:                   # noqa: BLE001
            # live 拿不到不代表适配层挂了，分开标注。
            live = None
            out['data']['ccpx_live_error'] = str(exc)
        out['data']['ccpx'] = {'status': 'up', 'status_raw': status, 'live': live}
    except Exception as exc:                       # noqa: BLE001
        out['data']['ccpx'] = {'status': 'down', 'error': str(exc)}

    # ---- 平台自身 ----
    out['data']['platform'] = {
        'status': 'up',
        'accounts': db.scalar(select(func.count()).select_from(Account)) or 0,
        'kami': db.scalar(select(func.count()).select_from(Kami)) or 0,
        'kamiApps': db.scalar(select(func.count()).select_from(KamiApp)) or 0,
        'kamiServers': db.scalar(select(func.count()).select_from(KamiServer)) or 0,
        'kamiUsers': db.scalar(select(func.count()).select_from(KamiUser)) or 0,
    }

    # 有任一下游不可用就标 degraded，但仍然是 200 —— 前端要能显示「部分可用」。
    down = [k for k, v in out['data'].items() if v.get('status') != 'up']
    out['degraded'] = bool(down)
    out['down'] = down
    return out
