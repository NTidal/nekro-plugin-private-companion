"""WebUI 路由：私人陪伴面板后端接口

挂载在 /plugins/xiaojiu.private_companion/ 下，页面与 API 均为相对路径。
模式参考 nekro_plugin_prompt_injector（mount_router + FileResponse）。
"""

import inspect
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from . import core
from .plugin import get_config, plugin
from .proactive import pick_motivation, should_send, trigger_proactive
from .state import (
    build_inject_text,
    ensure_daily_state,
    generate_daily_plan,
    generate_dream,
    generate_today_state,
    current_plan_event,
)
# ========== 请求模型 ==========


class UserUpdateRequest(BaseModel):
    user_id: str = Field(..., description="陪伴对象 QQ")
    action: str = Field(..., description="enable | disable | reset_quota | reset_ignored | save_profile")
    remark: str = Field("", description="后台备注名")
    nickname: str = Field("", description="聊天称呼")


class RegenerateRequest(BaseModel):
    target: str = Field(..., description="plan | state | dream")
    min_segments: Optional[int] = Field(None, description="仅 plan：临时覆盖日程时段数下限（3-16），留空用插件配置")
    max_segments: Optional[int] = Field(None, description="仅 plan：临时覆盖日程时段数上限（3-20），留空用插件配置")


class ProactiveTestRequest(BaseModel):
    user_id: str = Field(..., description="陪伴对象 QQ")


# ========== 工具 ==========


async def _maybe_await(value):
    """兼容契约函数同步/异步两种实现"""
    if inspect.isawaitable(value):
        return await value
    return value


def _norm_should_send(res) -> tuple:
    """归一化 should_send 的返回为 (bool, reason)"""
    if isinstance(res, (tuple, list)) and len(res) >= 2:
        return bool(res[0]), str(res[1])
    if isinstance(res, dict):
        ok = res.get("should_send", res.get("ok", False))
        return bool(ok), str(res.get("reason", ""))
    return bool(res), ""


def _disabled_response() -> Optional[JSONResponse]:
    """插件被禁用时返回 503 风格 JSON，正常时返回 None"""
    enabled = getattr(plugin, "is_enabled", True)
    if callable(enabled):
        enabled = enabled()
    if not enabled:
        return JSONResponse(status_code=503, content={"error": "插件已禁用"})
    return None


# ========== 路由 ==========


def _build_auth_dependencies() -> list:
    """复用 nekro 主 WebUI 的 JWT 鉴权（管理员登录态）；鉴权模块不可用时失败关闭"""
    try:
        from fastapi import Depends
        from nekro_agent.services.user.deps import get_current_active_user

        return [Depends(get_current_active_user)]
    except Exception as e:  # pragma: no cover
        from nekro_agent.api.core import logger

        logger.error(f"[private_companion] 鉴权依赖加载失败，API 将全部拒绝访问: {e!r}")

        async def _deny():
            from fastapi import HTTPException

            raise HTTPException(status_code=503, detail="鉴权模块不可用")

        from fastapi import Depends

        return [Depends(_deny)]


@plugin.mount_router()
def create_router() -> APIRouter:
    router = APIRouter()
    # API 子路由：全部要求 nekro 登录态（与主 WebUI 同一 JWT）；HTML 页面本身不拦（数据都在 API 里）
    api_router = APIRouter(dependencies=_build_auth_dependencies())

    # ---------- WebUI 页面 ----------

    @router.get("/", summary="WebUI 面板", include_in_schema=False)
    async def serve_webui():
        html_path = Path(__file__).parent / "webui.html"
        if not html_path.exists():
            return JSONResponse(status_code=404, content={"error": "webui.html 不存在"})
        # 禁止缓存，避免改版后浏览器仍跑旧页面
        return FileResponse(
            str(html_path),
            media_type="text/html",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"},
        )

    # ---------- 总览 ----------

    @api_router.get("/api/overview", summary="面板总览")
    async def api_overview():
        resp = _disabled_response()
        if resp:
            return resp
        try:
            cfg = get_config()
            bot_state = await ensure_daily_state()
            plan = bot_state.get("plan") or {}
            st = bot_state.get("state") or {}
            dream = bot_state.get("dream") or {}
            diaries = bot_state.get("diaries") or []
            return {
                "date": bot_state.get("date", ""),
                "plan_summary": plan.get("summary", ""),
                "current_energy": st.get("energy", ""),
                "mood": st.get("mood", ""),
                "conditions": st.get("conditions", []),
                "dream_afterglow": dream.get("afterglow", ""),
                "latest_diary_date": (diaries[-1].get("date", "") if diaries else ""),
                "target_user_count": len(core.target_user_ids()),
                "token_usage_today": await core.get_token_usage_today(),
                "proactive_enabled": cfg.PROACTIVE_ENABLED,
                "inject_enabled": cfg.INJECT_ENABLED,
            }
        except Exception as e:
            return {"error": str(e)}

    @api_router.get("/api/bot-state", summary="完整生活状态")
    async def api_bot_state():
        resp = _disabled_response()
        if resp:
            return resp
        try:
            return await ensure_daily_state()
        except Exception as e:
            return {"error": str(e)}

    # ---------- 陪伴对象 ----------

    @api_router.get("/api/users", summary="陪伴对象列表")
    async def api_users():
        resp = _disabled_response()
        if resp:
            return resp
        try:
            cfg = get_config()
            bot_state = await ensure_daily_state()
            users = []
            for uid in core.target_user_ids():
                user_state = await core.get_user_state(uid)
                ok, reason = False, ""
                try:
                    res = await _maybe_await(should_send(uid, user_state, bot_state))
                    ok, reason = _norm_should_send(res)
                except Exception as e:  # noqa: PERF203
                    reason = f"判定失败: {e}"
                item = dict(user_state)
                item["should_send_now"] = ok
                item["reason"] = reason
                item["quota_max"] = cfg.MAX_DAILY_MESSAGES
                users.append(item)
            return users
        except Exception as e:
            return {"error": str(e)}

    @api_router.post("/api/user/update", summary="修改陪伴对象状态")
    async def api_user_update(req: UserUpdateRequest):
        resp = _disabled_response()
        if resp:
            return resp
        try:
            user_state = await core.get_user_state(req.user_id)
            if req.action == "enable":
                user_state["enabled"] = True
            elif req.action == "disable":
                user_state["enabled"] = False
            elif req.action == "reset_quota":
                user_state["quota_date"] = core.today_key()
                user_state["quota_used"] = 0
            elif req.action == "reset_ignored":
                user_state["ignored_streak"] = 0
            elif req.action == "save_profile":
                user_state["remark"] = " ".join(str(req.remark or "").split())[:40]
                user_state["nickname"] = " ".join(str(req.nickname or "").split())[:24]
            else:
                return {"error": f"未知操作: {req.action}"}
            await core.save_user_state(req.user_id, user_state)
            return user_state
        except Exception as e:
            return {"error": str(e)}

    # ---------- 生成操作 ----------

    @api_router.post("/api/state/regenerate", summary="重新生成生活内容")
    async def api_regenerate(req: RegenerateRequest):
        resp = _disabled_response()
        if resp:
            return resp
        try:
            if req.target == "plan":
                result = await generate_daily_plan(
                    force=True,
                    min_segments=req.min_segments,
                    max_segments=req.max_segments,
                )
            elif req.target == "state":
                result = await generate_today_state(force=True)
            elif req.target == "dream":
                result = await generate_dream()
            else:
                return {"error": f"未知目标: {req.target}"}
            return {"success": result is not None, "target": req.target, "result": result}
        except Exception as e:
            return {"error": str(e)}

    @api_router.post("/api/proactive/test", summary="立即触发主动陪伴")
    async def api_proactive_test(req: ProactiveTestRequest):
        resp = _disabled_response()
        if resp:
            return resp
        try:
            user_state = await core.get_user_state(req.user_id)
            bot_state = await ensure_daily_state()
            motivation = await pick_motivation(req.user_id, user_state, bot_state)
            success = await trigger_proactive(req.user_id, motivation, manual=True)
            return {"success": bool(success), "motivation": motivation}
        except Exception as e:
            return {"error": str(e)}

    # ---------- 注入预览 ----------

    @api_router.get("/api/inject-preview", summary="生活状态注入预览")
    async def api_inject_preview():
        resp = _disabled_response()
        if resp:
            return resp
        try:
            return {"text": await build_inject_text()}
        except Exception as e:
            return {"error": str(e)}

    # ---------- 人设包：整套陪伴人设的导入 / 导出 ----------
    # 角色内容（世界观 / 场景池 / 事件池 / 触发别名）全部存插件数据目录：
    #   world_stage.txt、day_card.json、trigger_aliases.json
    # 这三个文件的格式与导出包里的 companion/* 完全一致，所以
    # 导出=读文件打包；导入=校验后写回文件（失败整体回滚）。均不再改写 .py 源码。

    def _plugin_dir() -> Path:
        return Path(__file__).parent

    def _backup_root() -> Path:
        try:
            from nekro_agent.core.os_env import OsEnv

            root = Path(OsEnv.DATA_DIR) / "plugin_data" / "xiaojiu.private_companion" / "companion_backups"
        except Exception:  # noqa: BLE001
            root = _plugin_dir() / "_companion_backups"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _collect_content() -> dict:
        """把当前生效的角色内容读成纯数据。"""
        from . import day_card as _dc
        from . import proactive as _pa

        card = _dc.current_card()
        return {
            "world_stage": core.load_world_stage(),
            "cooldown_days": card["cooldown_days"],
            "scenes": card["scenes"],
            "events": card["events"],
            "trigger_aliases": _pa.current_aliases(),
        }

    def _py_literal(obj) -> str:
        import json as _json

        return _json.dumps(obj, ensure_ascii=False, indent=4)

    @api_router.get("/api/backup/export", summary="导出整套陪伴人设（zip）")
    async def api_backup_export():
        resp = _disabled_response()
        if resp:
            return resp
        import io as _io
        import zipfile as _zipfile
        from datetime import datetime as _dt

        from fastapi.responses import Response as _Resp

        try:
            data = _collect_content()
            cfg = get_config()
            buf = _io.BytesIO()
            stamp = _dt.now().strftime("%Y%m%d_%H%M%S")
            meta = {
                "bundle_version": 1,
                "plugin": "xiaojiu.private_companion",
                "version": str(getattr(plugin, "version", "") or ""),
                "exported_at": _dt.now().isoformat(timespec="seconds"),
                "character_hint": (data["world_stage"][:60] + "…") if data["world_stage"] else "",
                "counts": {
                    "world_stage_chars": len(data["world_stage"]),
                    "scenes": len(data["scenes"]),
                    "events": len(data["events"]),
                    "trigger_aliases": len(data["trigger_aliases"]),
                },
            }
            with _zipfile.ZipFile(buf, "w", _zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("companion/world_stage.txt", data["world_stage"])
                zf.writestr(
                    "companion/day_card.json",
                    _py_literal(
                        {
                            "cooldown_days": data["cooldown_days"],
                            "scenes": data["scenes"],
                            "events": data["events"],
                        }
                    ),
                )
                zf.writestr("companion/trigger_aliases.json", _py_literal(data["trigger_aliases"]))
                zf.writestr(
                    "companion/config.json",
                    _py_literal(
                        {
                            "PERSONA_PRESET_ID": str(getattr(cfg, "PERSONA_PRESET_ID", "") or ""),
                            "PERSONA_PROMPT": str(getattr(cfg, "PERSONA_PROMPT", "") or ""),
                            "PLAN_STYLE_HINT": str(getattr(cfg, "PLAN_STYLE_HINT", "") or ""),
                        }
                    ),
                )
                zf.writestr("companion.bundle.json", _py_literal(meta))
            payload = buf.getvalue()
            return _Resp(
                content=payload,
                media_type="application/zip",
                headers={
                    "Content-Disposition": f'attachment; filename="companion_bundle_{stamp}.zip"'
                },
            )
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}

    @api_router.post("/api/backup/import", summary="导入整套陪伴人设（zip）")
    async def api_backup_import(request: Request):
        resp = _disabled_response()
        if resp:
            return resp
        import io as _io
        import json as _json
        import shutil as _shutil
        import zipfile as _zipfile
        from datetime import datetime as _dt

        body = await request.body()
        if not body:
            return {"error": "没有收到文件内容"}
        try:
            zf = _zipfile.ZipFile(_io.BytesIO(body))
        except Exception as e:  # noqa: BLE001
            return {"error": f"不是有效的 zip 包：{e}"}
        names = set(zf.namelist())

        def _pick(arc: str):
            for cand in (f"companion/{arc}", arc):
                if cand in names:
                    return cand
            return None

        try:
            # ---- 解析与校验（任何一项不合法都整体拒绝）----
            ws = ""
            ws_src = _pick("world_stage.txt")
            if ws_src:
                ws = zf.read(ws_src).decode("utf-8")
            card = None
            card_src = _pick("day_card.json")
            if card_src:
                card = _json.loads(zf.read(card_src).decode("utf-8"))
                if not isinstance(card, dict) or not isinstance(card.get("scenes"), list) \
                        or not isinstance(card.get("events"), list):
                    return {"error": "day_card.json 结构不对（需要 scenes / events 数组）"}
                for key in ("scenes", "events"):
                    for item in card[key]:
                        if not isinstance(item, dict) or not item.get("id"):
                            return {"error": f"day_card.json 的 {key} 里有条目缺少 id"}
            aliases = None
            al_src = _pick("trigger_aliases.json")
            if al_src:
                aliases = _json.loads(zf.read(al_src).decode("utf-8"))
                if not isinstance(aliases, dict):
                    return {"error": "trigger_aliases.json 结构不对（需要对象）"}
            if not ws and card is None and aliases is None:
                return {"error": "包里没有可导入的人设内容（world_stage.txt / day_card.json / trigger_aliases.json）"}

            # ---- 备份当前数据文件 ----
            stamp = _dt.now().strftime("%Y%m%d_%H%M%S")
            bdir = _backup_root() / stamp
            bdir.mkdir(parents=True, exist_ok=True)
            from . import day_card as _dc_mod
            from . import proactive as _pa_mod

            paths = {
                "world_stage.txt": core.world_stage_path(),
                "day_card.json": _dc_mod.card_path(),
                "trigger_aliases.json": _pa_mod.aliases_path(),
            }
            for key, path in paths.items():
                if path.is_file():
                    _shutil.copy2(path, bdir / key)

            # ---- 落盘（失败则整体回滚到备份；原本不存在的删掉）----
            written = []
            try:
                if ws.strip():
                    core.save_world_stage(ws)
                    written.append("world_stage.txt")
                if card is not None:
                    _dc_mod.save_card({
                        "cooldown_days": int(card.get("cooldown_days") or _dc_mod.COOLDOWN_DAYS or 7),
                        "scenes": card["scenes"],
                        "events": card["events"],
                    })
                    written.append("day_card.json")
                if aliases is not None:
                    _pa_mod.save_aliases(aliases)
                    written.append("trigger_aliases.json")
            except Exception:
                for key in written:
                    bak = bdir / key
                    if bak.is_file():
                        _shutil.copy2(bak, paths[key])
                    else:
                        paths[key].unlink(missing_ok=True)
                raise

            # 三个数据文件都按 (mtime, size) 缓存，落盘即生效，无需 reload 模块
            reloaded, reload_err = list(written), ""

            # ---- 可选：同步陪伴人格文本 ----
            cfg_applied = []
            cfg_src = _pick("config.json")
            if cfg_src:
                try:
                    cdata = _json.loads(zf.read(cfg_src).decode("utf-8"))
                    cfg = get_config()
                    for key in ("PERSONA_PRESET_ID", "PERSONA_PROMPT", "PLAN_STYLE_HINT"):
                        val = str(cdata.get(key) or "")
                        if val.strip():
                            setattr(cfg, key, val)
                            cfg_applied.append(key)
                    if cfg_applied:
                        plugin.save_config(cfg)
                except Exception:  # noqa: BLE001
                    pass

            return {
                "success": True,
                "applied": written,
                "reloaded": reloaded,
                "config_applied": cfg_applied,
                "backup_dir": str(bdir),
                "reload_error": reload_err,
            }
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}

    # ---------- 角色内容挂载：WORLD_STAGE / 场景池 / 事件池 ----------

    def _content_backup_root() -> Path:
        try:
            from nekro_agent.core.os_env import OsEnv

            root = Path(OsEnv.DATA_DIR) / "plugin_data" / "xiaojiu.private_companion" / "content_backups"
        except Exception:  # noqa: BLE001
            root = Path(__file__).parent / "_content_backups"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _read_content() -> dict:
        from . import core as _core
        from . import day_card as _dc

        card = _dc.current_card()
        return {
            "world_stage": _core.load_world_stage(),
            "cooldown_days": card["cooldown_days"],
            "scenes": card["scenes"],
            "events": card["events"],
        }

    def _py_literal(obj) -> str:
        import json as _json

        return _json.dumps(obj, ensure_ascii=False, indent=4)

    def _validate_pool(items, kind: str) -> list:
        if not isinstance(items, list):
            raise ValueError(f"{kind} 必须是数组")
        out, ids = [], set()
        for it in items:
            if not isinstance(it, dict):
                raise ValueError(f"{kind} 里有非对象条目")
            entry = {
                "id": str(it.get("id") or "").strip()[:48],
                ("title" if kind == "SCENES" else "blurb"): str(
                    it.get("title") if kind == "SCENES" else it.get("blurb") or ""
                ).strip()[:120],
            }
            if kind == "SCENES":
                entry["setting"] = str(it.get("setting") or "").strip()[:600]
            if not entry["id"]:
                raise ValueError(f"{kind} 里有条目缺少 id")
            if entry["id"] in ids:
                raise ValueError(f"{kind} 里 id 重复：{entry['id']}")
            ids.add(entry["id"])
            if kind == "SCENES" and not entry["setting"]:
                raise ValueError(f"场景 {entry['id']} 缺少 setting 描述")
            if kind == "EVENTS" and not entry["blurb"]:
                raise ValueError(f"事件 {entry['id']} 缺少 blurb 描述")
            out.append(entry)
        if not out:
            raise ValueError(f"{kind} 不能为空")
        return out

    @api_router.get("/api/content", summary="角色内容（世界观 + 场景池 + 事件池）")
    async def api_content_get():
        resp = _disabled_response()
        if resp:
            return resp
        try:
            return {"content": _read_content()}
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}

    @api_router.post("/api/content/world", summary="保存世界观舞台")
    async def api_content_world(request: Request):
        resp = _disabled_response()
        if resp:
            return resp
        import shutil as _shutil
        from datetime import datetime as _dt
        try:
            body = await request.json()
            ws = str(body.get("world_stage") or "").strip()
            if not ws:
                return {"error": "世界观文本不能为空"}
            if len(ws) > 4000:
                return {"error": "世界观文本超过 4000 字上限"}
            stamp = _dt.now().strftime("%Y%m%d_%H%M%S")
            bdir = _content_backup_root() / stamp
            bdir.mkdir(parents=True, exist_ok=True)
            old_fp = core.world_stage_path()
            if old_fp.is_file():
                _shutil.copy2(old_fp, bdir / "world_stage.txt")
            path = core.save_world_stage(ws)
            # 数据文件按 mtime 缓存，落盘即生效，无需 reload 模块
            return {
                "success": True,
                "persisted": True,
                "reloaded": True,
                "path": path,
                "backup_dir": str(bdir),
                "chars": len(ws),
            }
        except ValueError as e:
            return {"error": str(e)}
        except Exception as e:  # noqa: BLE001
            return {"error": f"保存失败：{e}"}

    @api_router.post("/api/content/pool", summary="保存场景池或事件池")
    async def api_content_pool(request: Request):
        resp = _disabled_response()
        if resp:
            return resp
        import shutil as _shutil
        from datetime import datetime as _dt
        try:
            body = await request.json()
            kind = str(body.get("kind") or "").upper()
            if kind not in ("SCENES", "EVENTS"):
                return {"error": "kind 必须是 SCENES 或 EVENTS"}
            items = _validate_pool(body.get("items"), kind)
            from . import day_card as _dc_mod

            cur = _dc_mod.current_card()
            if kind == "SCENES":
                cur["scenes"] = items
            else:
                cur["events"] = items
            stamp = _dt.now().strftime("%Y%m%d_%H%M%S")
            bdir = _content_backup_root() / stamp
            bdir.mkdir(parents=True, exist_ok=True)
            old_fp = _dc_mod.card_path()
            if old_fp.is_file():
                _shutil.copy2(old_fp, bdir / "day_card.json")
            path = _dc_mod.save_card(cur)
            return {
                "success": True,
                "persisted": True,
                "reloaded": True,
                "path": path,
                "backup_dir": str(bdir),
                "count": len(items),
            }
        except ValueError as e:
            return {"error": str(e)}
        except Exception as e:  # noqa: BLE001
            return {"error": f"保存失败：{e}"}

    router.include_router(api_router)
    return router