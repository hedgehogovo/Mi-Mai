from __future__ import annotations

import asyncio
import base64
import copy
import importlib
import io
import json
import shlex
import threading
import time
from pathlib import Path
from typing import Any

from maibot_sdk import API, Command, ConfigWidget, ConfigWidgetState, MaiBotPlugin, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

try:
    from .core import (
        DEFAULT_SETTINGS,
        DEVICE_CATEGORIES,
        device_is_allowed,
        display_device,
        display_scene,
        extract_user_identity,
        maiweave_should_defer,
        normalize_user_id,
        parse_action_arguments,
        parse_property_value,
        public_property,
        resolve_device,
        resolve_scene,
        scene_is_allowed,
        user_is_allowed,
    )
except ImportError:
    from core import (
        DEFAULT_SETTINGS,
        DEVICE_CATEGORIES,
        device_is_allowed,
        display_device,
        display_scene,
        extract_user_identity,
        maiweave_should_defer,
        normalize_user_id,
        parse_action_arguments,
        parse_property_value,
        public_property,
        resolve_device,
        resolve_scene,
        scene_is_allowed,
        user_is_allowed,
    )


REFUSAL_BUSY = "我这会儿有点忙，等我缓过来再弄吧。"
REFUSAL_NOT_IN_MOOD = "今天先别让我折腾这些啦，改天再说。"
REFUSAL_UNAVAILABLE = "我现在顾不上看这些，晚点再问我吧。"
REFUSAL_FAILED = "刚刚没弄好，我先缓一缓，等会儿再试。"
QR_LIFETIME_SECONDS = 120
READ_PROPERTY_LIMIT = 24


class MiMaiPlugin(MaiBotPlugin):
    config_widgets = [
        ConfigWidget(id="webui_hint", kind="status", label="配置入口"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._settings_lock = threading.RLock()
        self._api_lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._settings: dict[str, Any] = copy.deepcopy(DEFAULT_SETTINGS)
        self._api: Any = None
        self._spec_cache: dict[str, dict[str, Any]] = {}
        self._login_state: dict[str, Any] = {
            "status": "未登录",
            "pending": False,
            "qr_data_url": "",
            "expires_at": 0,
        }
        self._snapshot: dict[str, Any] = {
            "devices": [],
            "scenes": [],
            "refreshed_at": 0,
            "refreshing": False,
            "refresh_message": "",
        }
        self._jobs_lock = threading.RLock()
        self._login_thread: threading.Thread | None = None
        self._refresh_thread: threading.Thread | None = None
        self._control_thread: threading.Thread | None = None
        self._closing = threading.Event()
        self._data_dir: Path | None = None
        self._settings_path: Path | None = None
        self._auth_path: Path | None = None

    async def on_load(self) -> None:
        data_dir = Path(self.ctx.paths.data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        self._data_dir = data_dir
        self._settings_path = data_dir / "settings.json"
        self._auth_path = data_dir / "auth.json"
        self._load_settings()
        await self._sync_tool_components(include_read_tools=True)

        if self._auth_path.exists():
            self._login_state.update({"status": "已保存米家登录信息", "pending": False})
        self.ctx.logger.info("Mi Mai 已加载")

    async def on_unload(self) -> None:
        self._closing.set()
        for thread in (self._login_thread, self._refresh_thread, self._control_thread):
            if thread and thread.is_alive():
                thread.join(timeout=1.0)
        self.ctx.logger.info("Mi Mai 已卸载")

    async def on_config_update(
        self,
        scope: str,
        config_data: dict[str, object],
        version: str,
    ) -> None:
        del scope, config_data, version

    async def get_config_widget_state(self) -> dict[str, ConfigWidgetState]:
        return {
            "webui_hint": ConfigWidgetState(
                text="请打开 Mi Mai 侧栏 WebUI 完成扫码登录、设备白名单和控制权限配置。",
                tone="neutral",
            )
        }

    def _load_settings(self) -> None:
        if self._settings_path is None:
            return
        loaded: dict[str, Any] = {}
        try:
            if self._settings_path.exists():
                loaded = json.loads(self._settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.ctx.logger.exception("读取 Mi Mai 设置失败，将使用默认值")

        settings = copy.deepcopy(DEFAULT_SETTINGS)
        if isinstance(loaded, dict):
            for key in (
                "control_tools_enabled",
                "scene_tools_enabled",
                "only_whitelisted_users",
                "maiweave_enabled",
            ):
                if isinstance(loaded.get(key), bool):
                    settings[key] = loaded[key]
            if isinstance(loaded.get("allowed_users"), list):
                settings["allowed_users"] = sorted(
                    {
                        normalize_user_id(str(item))
                        for item in loaded["allowed_users"]
                        if isinstance(item, str) and normalize_user_id(item)
                    }
                )
            if isinstance(loaded.get("devices"), dict):
                settings["devices"] = loaded["devices"]
            if isinstance(loaded.get("scenes"), dict):
                settings["scenes"] = loaded["scenes"]

        with self._settings_lock:
            self._settings = settings
        self._write_settings()

    def _write_settings(self) -> None:
        if self._settings_path is None:
            return
        with self._settings_lock:
            data = json.dumps(self._settings, ensure_ascii=False, indent=2)
        temporary_path = self._settings_path.with_suffix(".json.tmp")
        temporary_path.write_text(data, encoding="utf-8")
        temporary_path.replace(self._settings_path)

    def _settings_copy(self) -> dict[str, Any]:
        with self._settings_lock:
            return copy.deepcopy(self._settings)

    async def _sync_tool_components(self, *, include_read_tools: bool = False) -> bool:
        settings = self._settings_copy()
        desired: dict[str, bool] = {
            "mijia_control_device": settings["control_tools_enabled"],
            "mijia_run_device_action": settings["control_tools_enabled"],
            "mijia_control_scene": settings["scene_tools_enabled"],
        }
        if include_read_tools:
            desired = {
                "mijia_list_devices": True,
                "mijia_get_device_status": True,
                **desired,
            }
        succeeded = True
        for name, enabled in desired.items():
            try:
                if enabled:
                    result = await self.ctx.component.enable_component(
                        name,
                        "tool",
                        scope="global",
                    )
                else:
                    result = await self.ctx.component.disable_component(
                        name,
                        "tool",
                        scope="global",
                    )
                if not result:
                    self.ctx.logger.warning("工具启用状态未更新: %s", name)
                succeeded = bool(result) and succeeded
            except Exception:
                succeeded = False
                self.ctx.logger.warning(
                    "更新工具启用状态失败: %s",
                    name,
                    exc_info=True,
                )
        return succeeded

    def _update_settings(self, mutate: Any) -> None:
        with self._settings_lock:
            mutate(self._settings)
            data = json.dumps(self._settings, ensure_ascii=False, indent=2)
        if self._settings_path is None:
            raise RuntimeError("插件数据目录尚未就绪")
        temporary_path = self._settings_path.with_suffix(".json.tmp")
        temporary_path.write_text(data, encoding="utf-8")
        temporary_path.replace(self._settings_path)

    def _new_api(self) -> Any:
        if self._data_dir is None:
            raise RuntimeError("插件数据目录尚未就绪")
        try:
            module = importlib.import_module("mijiaAPI")
        except ImportError as exc:
            raise RuntimeError("缺少 mijiaAPI 依赖") from exc
        client_type = getattr(module, "mijiaAPI")
        return client_type(str(self._data_dir))

    def _connected_api(self) -> Any:
        with self._api_lock:
            if self._api is None:
                self._api = self._new_api()
            if not getattr(self._api, "auth_data", None):
                raise RuntimeError("米家账号尚未登录")
            return self._api

    def _run_mijia(self, callback: Any) -> Any:
        with self._api_lock:
            api = self._connected_api()
            return callback(api)

    async def _run_mijia_async(self, callback: Any) -> Any:
        return await asyncio.to_thread(self._run_mijia, callback)

    def _log_exception(self, message: str) -> None:
        self.ctx.logger.exception(message)

    def _get_spec(self, spec_type: str) -> dict[str, Any]:
        if spec_type in self._spec_cache:
            return self._spec_cache[spec_type]
        module = importlib.import_module("mijiaAPI")
        getter = getattr(module, "get_device_info")
        raw = getter(spec_type)
        if not isinstance(raw, dict):
            raise RuntimeError("设备规格格式无效")

        properties = list(raw.get("properties") or [])
        actions = list(raw.get("actions") or [])
        for service in raw.get("services") or []:
            if not isinstance(service, dict):
                continue
            properties.extend(service.get("properties") or [])
            actions.extend(service.get("actions") or [])
        spec = {"properties": properties, "actions": actions}
        self._spec_cache[spec_type] = spec
        return spec

    def _model_device_capabilities(self, device: dict[str, Any]) -> dict[str, Any]:
        try:
            spec = self._get_spec(str(device.get("spec_type") or ""))
        except Exception:
            return {"writable_properties": [], "actions": []}
        writable_properties = [
            public_property(prop)
            for prop in spec["properties"]
            if isinstance(prop, dict) and self._is_writable(prop)
        ][:READ_PROPERTY_LIMIT]
        actions = [
            {
                "name": str(action.get("name") or ""),
                "description": str(action.get("description") or ""),
                "inputs": [
                    public_property(parameter)
                    for parameter in (
                        action.get("in") or action.get("inputs") or []
                    )
                    if isinstance(parameter, dict)
                ],
            }
            for action in spec["actions"]
            if isinstance(action, dict) and self._method_ids(action, "action")
        ][:READ_PROPERTY_LIMIT]
        return {"writable_properties": writable_properties, "actions": actions}

    @staticmethod
    def _method_ids(item: dict[str, Any], method_key: str) -> tuple[int, int] | None:
        method = item.get(method_key) or item.get("method")
        if isinstance(method, dict):
            siid = method.get("siid")
            iid = method.get("piid") if method_key == "property" else method.get("aiid")
            if siid is None:
                siid = item.get("siid")
            if iid is None:
                iid = item.get("piid") if method_key == "property" else item.get("aiid")
            try:
                return int(siid), int(iid)
            except (TypeError, ValueError):
                return None
        try:
            siid = item.get("siid")
            iid = item.get("piid") if method_key == "property" else item.get("aiid")
            return int(siid), int(iid)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _is_readable(prop: dict[str, Any]) -> bool:
        rw = str(prop.get("rw") or "").lower()
        return "read" in rw or "r" == rw

    @staticmethod
    def _is_writable(prop: dict[str, Any]) -> bool:
        rw = str(prop.get("rw") or "").lower()
        return "write" in rw or "w" == rw or "rw" == rw

    @staticmethod
    def _json_safe(value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, list):
            return [MiMaiPlugin._json_safe(item) for item in value]
        if isinstance(value, dict):
            return {str(key): MiMaiPlugin._json_safe(item) for key, item in value.items()}
        return str(value)

    def _read_device(self, api: Any, device: dict[str, Any]) -> dict[str, Any]:
        item = dict(device)
        item["properties"] = []
        item["actions"] = []
        item["states"] = []
        item["status_error"] = False
        spec_type = str(item.get("spec_type") or "")
        if not spec_type:
            return item
        try:
            spec = self._get_spec(spec_type)
            properties = [
                prop for prop in spec["properties"]
                if isinstance(prop, dict) and self._method_ids(prop, "property")
            ]
            item["properties"] = [
                {
                    **public_property(prop),
                    "writable": self._is_writable(prop),
                    "readable": self._is_readable(prop),
                }
                for prop in properties
            ]
            item["actions"] = [
                {
                    "name": str(action.get("name") or ""),
                    "description": str(action.get("description") or ""),
                    "inputs": [
                        public_property(parameter)
                        for parameter in (
                            action.get("in") or action.get("inputs") or []
                        )
                        if isinstance(parameter, dict)
                    ],
                }
                for action in spec["actions"]
                if isinstance(action, dict)
                and self._method_ids(action, "action")
            ]

            readable = [prop for prop in properties if self._is_readable(prop)]
            requests = []
            requested_props = []
            for prop in readable[:READ_PROPERTY_LIMIT]:
                method = self._method_ids(prop, "property")
                if method is None:
                    continue
                siid, piid = method
                requests.append({"did": str(item.get("did")), "siid": siid, "piid": piid})
                requested_props.append(prop)
            if requests:
                results = api.get_devices_prop(requests)
                if not isinstance(results, list):
                    results = [results]
                states = []
                for prop, result in zip(requested_props, results):
                    if not isinstance(result, dict) or result.get("code", 0) not in (0, 1):
                        continue
                    states.append(
                        public_property(
                            prop,
                            self._json_safe(result.get("value")),
                            "value" in result,
                        )
                    )
                item["states"] = states
        except Exception:
            item["status_error"] = True
            self.ctx.logger.warning(
                "读取设备状态失败: did=%s",
                item.get("did"),
                exc_info=True,
            )
        return item

    def _refresh_sync(self) -> None:
        with self._api_lock:
            api = self._connected_api()
            devices = api.get_devices_list()
            scenes = api.get_scenes_list()
            refreshed_devices = [
                self._read_device(api, item)
                for item in devices
                if isinstance(item, dict)
            ]
            clean_scenes = [
                {
                    "scene_id": str(item.get("scene_id") or ""),
                    "home_id": str(item.get("home_id") or ""),
                    "name": str(item.get("name") or "未命名场景"),
                }
                for item in scenes
                if isinstance(item, dict)
            ]
        with self._state_lock:
            self._snapshot = {
                "devices": refreshed_devices,
                "scenes": clean_scenes,
                "refreshed_at": int(time.time()),
                "refreshing": False,
                "refresh_message": "设备和场景已更新",
            }

    def _refresh_worker(self) -> None:
        try:
            self._refresh_sync()
        except Exception:
            self._log_exception("刷新米家设备失败")
            with self._state_lock:
                self._snapshot["refreshing"] = False
                self._snapshot["refresh_message"] = "读取失败，请确认账号状态和网络后重试"

    def _start_refresh(self) -> bool:
        with self._jobs_lock:
            if self._closing.is_set():
                return False
            if self._refresh_thread and self._refresh_thread.is_alive():
                return False
            with self._state_lock:
                self._snapshot["refreshing"] = True
                self._snapshot["refresh_message"] = "正在读取设备和场景…"
            self._refresh_thread = threading.Thread(
                target=self._refresh_worker,
                name="mi-mai-refresh",
                daemon=True,
            )
            self._refresh_thread.start()
            return True

    def _start_webui_control(self, label: str, operation: Any) -> bool:
        with self._jobs_lock:
            if self._closing.is_set():
                return False
            if self._control_thread and self._control_thread.is_alive():
                return False
            with self._state_lock:
                self._snapshot["refresh_message"] = f"正在执行{label}…"
            self._control_thread = threading.Thread(
                target=self._webui_control_worker,
                args=(label, operation),
                name="mi-mai-control",
                daemon=True,
            )
            self._control_thread.start()
            return True

    def _webui_control_worker(self, label: str, operation: Any) -> None:
        try:
            succeeded = bool(operation())
        except Exception:
            self._log_exception(f"侧栏{label}失败")
            succeeded = False
        if succeeded:
            with self._state_lock:
                self._snapshot["refresh_message"] = f"{label}已完成，正在更新设备状态"
            self._start_refresh()
        else:
            with self._state_lock:
                self._snapshot["refreshing"] = False
                self._snapshot["refresh_message"] = f"{label}未完成，请检查设备状态后重试"

    def _qr_data_url(self, login_url: str) -> str:
        module = importlib.import_module("mijiaAPI.apis")
        qr = module.QRCode(box_size=7, border=2)
        qr.add_data(login_url)
        image = qr.make_image(fill_color="black", back_color="white")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return "data:image/png;base64," + encoded

    def _login_worker(self) -> None:
        try:
            api = self._new_api()
            login_data = api._get_qr_login_data()
            if login_data.get("refreshed"):
                with self._api_lock:
                    self._api = api
                with self._state_lock:
                    self._login_state = {
                        "status": "米家登录已续期",
                        "pending": False,
                        "qr_data_url": "",
                        "expires_at": 0,
                    }
                self._start_refresh()
                return

            qr_data_url = self._qr_data_url(str(login_data["loginUrl"]))
            with self._state_lock:
                self._login_state = {
                    "status": "请使用米家 App 扫描二维码",
                    "pending": True,
                    "qr_data_url": qr_data_url,
                    "expires_at": int(time.time()) + QR_LIFETIME_SECONDS,
                }
            api._complete_qr_login(login_data)
            if self._closing.is_set():
                return
            with self._api_lock:
                self._api = api
            with self._state_lock:
                self._login_state = {
                    "status": "米家登录成功",
                    "pending": False,
                    "qr_data_url": "",
                    "expires_at": 0,
                }
            self._start_refresh()
        except Exception:
            self._log_exception("米家扫码登录失败")
            with self._state_lock:
                self._login_state = {
                    "status": "登录未完成，请重新获取二维码",
                    "pending": False,
                    "qr_data_url": "",
                    "expires_at": 0,
                }

    def _start_login(self) -> bool:
        with self._jobs_lock:
            if self._login_thread and self._login_thread.is_alive():
                return False
            if self._closing.is_set():
                return False
            with self._state_lock:
                self._login_state = {
                    "status": "正在准备二维码…",
                    "pending": True,
                    "qr_data_url": "",
                    "expires_at": 0,
                }
            self._login_thread = threading.Thread(
                target=self._login_worker,
                name="mi-mai-login",
                daemon=True,
            )
            self._login_thread.start()
            return True

    def _ui_snapshot(self) -> dict[str, Any]:
        settings = self._settings_copy()
        with self._state_lock:
            login = copy.deepcopy(self._login_state)
            snapshot = copy.deepcopy(self._snapshot)
        connected = bool(self._auth_path and self._auth_path.exists())
        if not connected and self._api is not None:
            connected = bool(getattr(self._api, "auth_data", None))
        login["connected"] = connected
        if connected and not login["pending"]:
            login["status"] = "米家账号已连接，可刷新设备"
        return {
            "login": login,
            "settings": {
                "control_tools_enabled": settings["control_tools_enabled"],
                "scene_tools_enabled": settings["scene_tools_enabled"],
                "only_whitelisted_users": settings["only_whitelisted_users"],
                "maiweave_enabled": settings["maiweave_enabled"],
                "allowed_users": list(settings["allowed_users"]),
            },
            "devices": [
                display_device(device, settings)
                for device in snapshot["devices"]
            ],
            "scenes": [
                display_scene(scene, settings)
                for scene in snapshot["scenes"]
            ],
            "refresh": {
                "refreshing": snapshot["refreshing"],
                "message": snapshot["refresh_message"],
                "refreshed_at": snapshot["refreshed_at"],
            },
            "categories": DEVICE_CATEGORIES,
        }

    def _control_denial(
        self,
        identity: str,
        *,
        scene: bool = False,
    ) -> str | None:
        settings = self._settings_copy()
        if scene:
            if not settings["scene_tools_enabled"]:
                return REFUSAL_NOT_IN_MOOD
        elif not settings["control_tools_enabled"]:
            return REFUSAL_NOT_IN_MOOD
        if not user_is_allowed(settings, identity):
            return REFUSAL_BUSY
        return None

    def _user_identity_from_context(self, kwargs: dict[str, Any]) -> str:
        contexts = []
        for key in (
            "message",
            "message_info",
            "message_context",
            "event",
            "event_data",
            "sender",
            "sender_info",
        ):
            if key in kwargs:
                contexts.append(kwargs[key])
        return extract_user_identity(*contexts)

    async def _is_maiweave_busy(self) -> bool:
        settings = self._settings_copy()
        if not settings["maiweave_enabled"]:
            return False
        try:
            response = await asyncio.wait_for(
                self.ctx.api.call(
                    "ilworkcn.maiweave.evaluate_behavior",
                    version="1",
                    behavior="proactive",
                ),
                timeout=3.0,
            )
        except Exception:
            self.ctx.logger.warning("读取 MaiWeave 状态失败，按普通状态继续")
            return False

        return maiweave_should_defer(response)

    async def _may_control(
        self,
        kwargs: dict[str, Any],
        *,
        scene: bool = False,
    ) -> str | None:
        denial = self._control_denial(self._user_identity_from_context(kwargs), scene=scene)
        if denial:
            return denial
        if await self._is_maiweave_busy():
            return REFUSAL_BUSY
        return None

    async def _get_property_values(
        self,
        device: dict[str, Any],
    ) -> list[dict[str, Any]]:
        did = str(device.get("did") or "")
        spec_type = str(device.get("spec_type") or "")
        if not did or not spec_type:
            return []

        def read(api: Any) -> list[dict[str, Any]]:
            spec = self._get_spec(spec_type)
            properties = [
                prop for prop in spec["properties"]
                if isinstance(prop, dict)
                and self._is_readable(prop)
                and self._method_ids(prop, "property")
            ][:READ_PROPERTY_LIMIT]
            requests = []
            for prop in properties:
                method = self._method_ids(prop, "property")
                if method:
                    requests.append(
                        {
                            "did": did,
                            "siid": method[0],
                            "piid": method[1],
                        }
                    )
            results = api.get_devices_prop(requests) if requests else []
            if not isinstance(results, list):
                results = [results]
            states = []
            for prop, result in zip(properties, results):
                if isinstance(result, dict) and result.get("code", 0) in (0, 1):
                    states.append(
                        public_property(
                            prop,
                            self._json_safe(result.get("value")),
                            "value" in result,
                        )
                    )
            return states

        return await self._run_mijia_async(read)

    async def _set_property(
        self,
        device: dict[str, Any],
        property_name: str,
        raw_value: str,
    ) -> bool:
        did = str(device.get("did") or "")
        spec_type = str(device.get("spec_type") or "")
        if not did or not spec_type:
            return False

        def write(api: Any) -> bool:
            spec = self._get_spec(spec_type)
            prop = next(
                (
                    item for item in spec["properties"]
                    if isinstance(item, dict)
                    and str(item.get("name") or "").casefold()
                    == property_name.strip().casefold()
                    and self._is_writable(item)
                ),
                None,
            )
            if prop is None:
                raise ValueError("该设备不支持此属性")
            method = self._method_ids(prop, "property")
            if method is None:
                raise ValueError("设备属性编号无效")
            value = parse_property_value(raw_value, prop)
            result = api.set_devices_prop(
                {
                    "did": did,
                    "siid": method[0],
                    "piid": method[1],
                    "value": value,
                }
            )
            return isinstance(result, dict) and result.get("code", 0) in (0, 1)

        return await self._run_mijia_async(write)

    async def _run_device_action(
        self,
        device: dict[str, Any],
        action_name: str,
        raw_arguments: str,
    ) -> bool:
        did = str(device.get("did") or "")
        spec_type = str(device.get("spec_type") or "")
        if not did or not spec_type:
            return False

        def run(api: Any) -> bool:
            spec = self._get_spec(spec_type)
            actions = [
                action for action in spec["actions"]
                if isinstance(action, dict)
                and str(action.get("name") or "").casefold()
                == action_name.strip().casefold()
            ]
            if len(actions) != 1:
                raise ValueError("设备动作不存在或名称不唯一")
            action = actions[0]
            method = self._method_ids(action, "action")
            if method is None:
                raise ValueError("设备动作编号无效")
            arguments = parse_action_arguments(raw_arguments, action)
            result = api.run_action(
                {
                    "did": did,
                    "siid": method[0],
                    "aiid": method[1],
                    "value": arguments,
                }
            )
            return isinstance(result, dict) and result.get("code", 0) in (0, 1)

        return await self._run_mijia_async(run)

    async def _run_scene(self, scene: dict[str, Any]) -> bool:
        scene_id = str(scene.get("scene_id") or "")
        home_id = str(scene.get("home_id") or "")
        if not scene_id or not home_id:
            return False
        return await self._run_mijia_async(
            lambda api: bool(api.run_scene(scene_id, home_id))
        )

    def _require_webui_device(self, did: str) -> dict[str, Any] | None:
        with self._state_lock:
            devices = copy.deepcopy(self._snapshot["devices"])
        return next(
            (device for device in devices if str(device.get("did")) == str(did)),
            None,
        )

    def _require_webui_scene(self, scene_key: str) -> dict[str, Any] | None:
        with self._state_lock:
            scenes = copy.deepcopy(self._snapshot["scenes"])
        for scene in scenes:
            key = f"{scene.get('home_id')}:{scene.get('scene_id')}"
            if key == scene_key:
                return scene
        return None

    @API("dashboard", description="获取 Mi Mai 侧栏页面状态", version="1")
    async def webui_dashboard(self) -> dict[str, Any]:
        return self._ui_snapshot()

    @API("start_login", description="开始米家扫码登录", version="1")
    async def webui_start_login(self) -> dict[str, Any]:
        started = self._start_login()
        return {"ok": True, "message": "正在准备二维码" if started else "登录任务正在进行"}

    @API("refresh_devices", description="刷新米家设备状态和场景", version="1")
    async def webui_refresh_devices(self) -> dict[str, Any]:
        if not self._auth_path or not self._auth_path.exists():
            return {"ok": False, "message": "请先完成米家登录"}
        started = self._start_refresh()
        return {"ok": True, "message": "正在刷新" if started else "刷新任务正在进行"}

    @API("save_device", description="保存设备别名、类别和白名单状态", version="1")
    async def webui_save_device(
        self,
        did: str,
        alias: str,
        category: str,
        allowed: bool,
    ) -> dict[str, Any]:
        device = self._require_webui_device(did)
        if not device:
            return {"ok": False, "message": "设备已不在当前列表，请先刷新"}
        alias = alias.strip()
        if allowed and (not alias or category not in DEVICE_CATEGORIES):
            return {"ok": False, "message": "请先填写别名并选择设备类别"}

        def mutate(settings: dict[str, Any]) -> None:
            settings["devices"][str(did)] = {
                "alias": alias,
                "category": category if category in DEVICE_CATEGORIES else "其他",
                "allowed": bool(allowed),
            }

        self._update_settings(mutate)
        return {"ok": True, "message": "设备配置已保存"}

    @API("set_scene_allowed", description="设置场景白名单", version="1")
    async def webui_set_scene_allowed(
        self,
        key: str,
        allowed: bool,
    ) -> dict[str, Any]:
        scene = self._require_webui_scene(key)
        if not scene:
            return {"ok": False, "message": "场景已不在当前列表，请先刷新"}

        def mutate(settings: dict[str, Any]) -> None:
            settings["scenes"][key] = bool(allowed)

        self._update_settings(mutate)
        return {"ok": True, "message": "场景白名单已更新"}

    @API("set_controls", description="启用或停用自然语言控制工具", version="1")
    async def webui_set_controls(
        self,
        device_enabled: bool,
        scene_enabled: bool,
    ) -> dict[str, Any]:
        def mutate(settings: dict[str, Any]) -> None:
            settings["control_tools_enabled"] = bool(device_enabled)
            settings["scene_tools_enabled"] = bool(scene_enabled)

        self._update_settings(mutate)
        if not await self._sync_tool_components():
            return {
                "ok": False,
                "message": "设置已保存，但工具列表暂未更新；请重启 MaiBot 后重试",
            }
        return {"ok": True, "message": "自然语言控制设置已更新"}

    @API("set_privacy", description="更新用户白名单控制策略", version="1")
    async def webui_set_privacy(self, only_whitelisted_users: bool) -> dict[str, Any]:
        self._update_settings(
            lambda settings: settings.__setitem__(
                "only_whitelisted_users",
                bool(only_whitelisted_users),
            )
        )
        return {"ok": True, "message": "用户控制范围已更新"}

    @API("set_maiweave", description="启用或停用 MaiWeave 状态联动", version="1")
    async def webui_set_maiweave(self, enabled: bool) -> dict[str, Any]:
        self._update_settings(
            lambda settings: settings.__setitem__("maiweave_enabled", bool(enabled))
        )
        return {"ok": True, "message": "MaiWeave 联动设置已更新"}

    @API("add_user", description="添加设备控制用户白名单", version="1")
    async def webui_add_user(self, identity: str) -> dict[str, Any]:
        normalized = normalize_user_id(identity)
        if not normalized or ":" not in normalized:
            return {
                "ok": False,
                "message": "请使用“平台:用户ID”格式，例如 qq:123456",
            }

        def mutate(settings: dict[str, Any]) -> None:
            if normalized not in settings["allowed_users"]:
                settings["allowed_users"].append(normalized)
                settings["allowed_users"].sort()

        self._update_settings(mutate)
        return {"ok": True, "message": "用户已加入白名单"}

    @API("remove_user", description="移除设备控制用户白名单", version="1")
    async def webui_remove_user(self, identity: str) -> dict[str, Any]:
        normalized = normalize_user_id(identity)

        def mutate(settings: dict[str, Any]) -> None:
            settings["allowed_users"] = [
                item for item in settings["allowed_users"] if item != normalized
            ]

        self._update_settings(mutate)
        return {"ok": True, "message": "用户已移出白名单"}

    @API("control_property", description="从侧栏设置已加入白名单的设备属性", version="1")
    async def webui_control_property(
        self,
        did: str,
        property_name: str,
        value: str,
    ) -> dict[str, Any]:
        settings = self._settings_copy()
        if not device_is_allowed(settings, did):
            return {"ok": False, "message": "请先为设备配置别名、类别并加入白名单"}
        device = self._require_webui_device(did)
        if not device:
            return {"ok": False, "message": "设备已不在当前列表，请先刷新"}
        if await self._is_maiweave_busy():
            return {"ok": False, "message": "现在不太方便操作，稍后再试吧"}
        started = self._start_webui_control(
            "设备操作",
            lambda: asyncio.run(self._set_property(device, property_name, value)),
        )
        if not started:
            return {"ok": False, "message": "上一项操作仍在进行，请稍后再试"}
        return {"ok": True, "message": "设备操作已提交，正在执行"}

    @API(
        "run_device_action",
        description="从侧栏运行已加入白名单的设备动作",
        version="1",
    )
    async def webui_run_device_action(
        self,
        did: str,
        action_name: str,
        arguments: str,
    ) -> dict[str, Any]:
        settings = self._settings_copy()
        if not device_is_allowed(settings, did):
            return {"ok": False, "message": "请先为设备配置别名、类别并加入白名单"}
        device = self._require_webui_device(did)
        if not device:
            return {"ok": False, "message": "设备已不在当前列表，请先刷新"}
        if await self._is_maiweave_busy():
            return {"ok": False, "message": "现在不太方便操作，稍后再试吧"}
        started = self._start_webui_control(
            "设备动作",
            lambda: asyncio.run(
                self._run_device_action(device, action_name, arguments)
            ),
        )
        if not started:
            return {"ok": False, "message": "上一项操作仍在进行，请稍后再试"}
        return {"ok": True, "message": "设备动作已提交，正在执行"}

    @API(
        "run_scene",
        description="从侧栏运行已加入白名单的米家场景",
        version="1",
    )
    async def webui_run_scene(self, key: str) -> dict[str, Any]:
        settings = self._settings_copy()
        scene = self._require_webui_scene(key)
        if not scene:
            return {"ok": False, "message": "场景已不在当前列表，请先刷新"}
        if not scene_is_allowed(settings, scene["scene_id"], scene["home_id"]):
            return {"ok": False, "message": "请先将场景加入白名单"}
        if await self._is_maiweave_busy():
            return {"ok": False, "message": "现在不太方便操作，稍后再试吧"}
        started = self._start_webui_control(
            "场景",
            lambda: asyncio.run(self._run_scene(scene)),
        )
        if not started:
            return {"ok": False, "message": "上一项操作仍在进行，请稍后再试"}
        return {"ok": True, "message": "场景操作已提交，正在执行"}

    @API("logout", description="清除本插件保存的米家登录信息", version="1")
    async def webui_logout(self) -> dict[str, Any]:
        with self._jobs_lock:
            if any(
                thread and thread.is_alive()
                for thread in (
                    self._login_thread,
                    self._refresh_thread,
                    self._control_thread,
                )
            ):
                return {"ok": False, "message": "当前操作仍在进行，请稍后重试"}
        if self._auth_path and self._auth_path.exists():
            self._auth_path.unlink()
        with self._api_lock:
            self._api = None
        with self._state_lock:
            self._snapshot = {
                "devices": [],
                "scenes": [],
                "refreshed_at": 0,
                "refreshing": False,
                "refresh_message": "",
            }
            self._login_state = {
                "status": "已退出米家账号",
                "pending": False,
                "qr_data_url": "",
                "expires_at": 0,
            }
        return {"ok": True, "message": "已退出米家账号"}

    @Tool(
        "mijia_list_devices",
        description="只读列出米家账号现有设备、设备类别与已配置别名。不执行设备控制。",
        core_tool=True,
        parameters=[
            ToolParameterInfo(
                name="stream_id",
                param_type=ToolParamType.STRING,
                description="当前聊天流 ID",
                required=True,
            ),
        ],
    )
    async def tool_list_devices(self, stream_id: str, **kwargs: Any) -> dict[str, Any]:
        del stream_id, kwargs
        try:
            with self._state_lock:
                if self._snapshot["devices"]:
                    devices = copy.deepcopy(self._snapshot["devices"])
                    settings = self._settings_copy()
                    return {
                        "success": True,
                        "devices": [
                            {
                                "name": device.get("name"),
                                "alias": settings["devices"].get(
                                    str(device.get("did")), {}
                                ).get("alias", ""),
                                "category": settings["devices"].get(
                                    str(device.get("did")), {}
                                ).get("category", ""),
                                "model": device.get("model", ""),
                                **self._model_device_capabilities(device),
                            }
                            for device in devices
                        ],
                        "content": "当前设备：" + "、".join(
                            settings["devices"].get(str(device.get("did")), {}).get(
                                "alias"
                            )
                            or device.get("name")
                            or "未命名设备"
                            for device in devices
                        ),
                    }
            devices = await self._run_mijia_async(lambda api: api.get_devices_list())
            settings = self._settings_copy()
            listed_devices = [
                {
                    "name": item.get("name", "未命名设备"),
                    "alias": settings["devices"].get(str(item.get("did")), {}).get(
                        "alias", ""
                    ),
                    "category": settings["devices"].get(
                        str(item.get("did")), {}
                    ).get("category", ""),
                    "model": item.get("model", ""),
                    **self._model_device_capabilities(item),
                }
                for item in devices
                if isinstance(item, dict)
            ]
            return {
                "success": True,
                "devices": listed_devices,
                "content": "当前设备：" + "、".join(
                    item.get("alias") or item.get("name") or "未命名设备"
                    for item in listed_devices
                ),
            }
        except Exception:
            self._log_exception("LLM 列出米家设备失败")
            return {
                "success": True,
                "message": REFUSAL_UNAVAILABLE,
                "content": REFUSAL_UNAVAILABLE,
            }

    @Tool(
        "mijia_get_device_status",
        description="只读查询一个米家设备当前可读属性状态，不执行任何控制。",
        core_tool=True,
        parameters=[
            ToolParameterInfo(
                name="stream_id",
                param_type=ToolParamType.STRING,
                description="当前聊天流 ID",
                required=True,
            ),
            ToolParameterInfo(
                name="device",
                param_type=ToolParamType.STRING,
                description="设备别名或米家设备名称",
                required=True,
            ),
        ],
    )
    async def tool_get_device_status(
        self,
        stream_id: str,
        device: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        del stream_id, kwargs
        try:
            with self._state_lock:
                cached_devices = copy.deepcopy(self._snapshot["devices"])
            if not cached_devices:
                raw_devices = await self._run_mijia_async(
                    lambda api: api.get_devices_list()
                )
                cached_devices = [
                    item for item in raw_devices if isinstance(item, dict)
                ]
            selected = resolve_device(
                cached_devices,
                self._settings_copy(),
                device,
            )
            if not selected:
                return {
                    "success": True,
                    "message": "我没找到你说的那个设备，换个名字问我吧。",
                    "content": "我没找到你说的那个设备，换个名字问我吧。",
                }
            states = await self._get_property_values(selected)
            preferences = self._settings_copy()["devices"].get(
                str(selected.get("did")), {}
            )
            return {
                "success": True,
                "device": preferences.get("alias") or selected.get("name"),
                "status": states,
                **self._model_device_capabilities(selected),
                "content": (
                    f"{preferences.get('alias') or selected.get('name')}："
                    + (
                        "；".join(
                            f"{item.get('description') or item.get('name')}: "
                            f"{item.get('value')}"
                            for item in states
                            if "value" in item
                        )
                        or "暂时没有读到状态"
                    )
                ),
            }
        except Exception:
            self._log_exception("LLM 查询米家设备状态失败")
            return {
                "success": True,
                "message": REFUSAL_UNAVAILABLE,
                "content": REFUSAL_UNAVAILABLE,
            }

    @Tool(
        "mijia_control_device",
        description=(
            "按用户明确要求设置一个米家设备的可写属性。"
            "仅对侧栏已配置别名、类别且加入白名单的设备生效；"
            "控制工具必须已由用户开启。"
        ),
        parameters=[
            ToolParameterInfo(
                name="stream_id",
                param_type=ToolParamType.STRING,
                description="当前聊天流 ID",
                required=True,
            ),
            ToolParameterInfo(
                name="device",
                param_type=ToolParamType.STRING,
                description="设备别名",
                required=True,
            ),
            ToolParameterInfo(
                name="property_name",
                param_type=ToolParamType.STRING,
                description="设备规格中的可写属性名称",
                required=True,
            ),
            ToolParameterInfo(
                name="value",
                param_type=ToolParamType.STRING,
                description="要设置的值；布尔值写 true/false，数字写十进制，文本直接填写",
                required=True,
            ),
        ],
    )
    async def tool_control_device(
        self,
        stream_id: str,
        device: str,
        property_name: str,
        value: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        del stream_id
        denial = await self._may_control(kwargs)
        if denial:
            return {"success": True, "message": denial, "content": denial}
        try:
            settings = self._settings_copy()
            with self._state_lock:
                devices = copy.deepcopy(self._snapshot["devices"])
            selected = resolve_device(devices, settings, device)
            if not selected:
                return {
                    "success": True,
                    "message": REFUSAL_NOT_IN_MOOD,
                    "content": REFUSAL_NOT_IN_MOOD,
                }
            if not device_is_allowed(settings, str(selected.get("did"))):
                return {
                    "success": True,
                    "message": REFUSAL_BUSY,
                    "content": REFUSAL_BUSY,
                }
            denial = self._control_denial(self._user_identity_from_context(kwargs))
            if denial:
                return {"success": True, "message": denial, "content": denial}
            succeeded = await self._set_property(selected, property_name, value)
            if not succeeded:
                return {
                    "success": True,
                    "message": REFUSAL_FAILED,
                    "content": REFUSAL_FAILED,
                }
            success_message = (
                f"好啦，{settings['devices'][str(selected['did'])]['alias']} 已经调好了。"
            )
            return {
                "success": True,
                "message": success_message,
                "content": success_message,
            }
        except ValueError:
            return {
                "success": True,
                "message": REFUSAL_NOT_IN_MOOD,
                "content": REFUSAL_NOT_IN_MOOD,
            }
        except Exception:
            self._log_exception("LLM 控制米家设备失败")
            return {
                "success": True,
                "message": REFUSAL_FAILED,
                "content": REFUSAL_FAILED,
            }

    @Tool(
        "mijia_run_device_action",
        description=(
            "按用户明确要求运行一个米家设备支持的 MIoT 动作。"
            "仅对白名单设备生效；设备控制工具必须已由用户开启。"
            "arguments 使用 JSON 数组，参数顺序按设备动作规格。"
        ),
        parameters=[
            ToolParameterInfo(
                name="stream_id",
                param_type=ToolParamType.STRING,
                description="当前聊天流 ID",
                required=True,
            ),
            ToolParameterInfo(
                name="device",
                param_type=ToolParamType.STRING,
                description="设备别名",
                required=True,
            ),
            ToolParameterInfo(
                name="action_name",
                param_type=ToolParamType.STRING,
                description="设备规格中的动作名称",
                required=True,
            ),
            ToolParameterInfo(
                name="arguments",
                param_type=ToolParamType.STRING,
                description="按设备动作规格排列的 JSON 参数数组；无参数时填写 []",
                required=True,
            ),
        ],
    )
    async def tool_run_device_action(
        self,
        stream_id: str,
        device: str,
        action_name: str,
        arguments: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        del stream_id
        denial = await self._may_control(kwargs)
        if denial:
            return {"success": True, "message": denial, "content": denial}
        try:
            settings = self._settings_copy()
            with self._state_lock:
                devices = copy.deepcopy(self._snapshot["devices"])
            selected = resolve_device(devices, settings, device)
            if not selected or not device_is_allowed(
                settings,
                str(selected.get("did")),
            ):
                return {
                    "success": True,
                    "message": REFUSAL_BUSY,
                    "content": REFUSAL_BUSY,
                }
            denial = self._control_denial(self._user_identity_from_context(kwargs))
            if denial:
                return {"success": True, "message": denial, "content": denial}
            succeeded = await self._run_device_action(
                selected,
                action_name,
                arguments,
            )
            if not succeeded:
                return {
                    "success": True,
                    "message": REFUSAL_FAILED,
                    "content": REFUSAL_FAILED,
                }
            success_message = (
                f"好啦，{settings['devices'][str(selected['did'])]['alias']} "
                f"的{action_name}已经执行了。"
            )
            return {
                "success": True,
                "message": success_message,
                "content": success_message,
            }
        except ValueError:
            return {
                "success": True,
                "message": REFUSAL_NOT_IN_MOOD,
                "content": REFUSAL_NOT_IN_MOOD,
            }
        except Exception:
            self._log_exception("LLM 执行米家设备动作失败")
            return {
                "success": True,
                "message": REFUSAL_FAILED,
                "content": REFUSAL_FAILED,
            }

    @Tool(
        "mijia_control_scene",
        description=(
            "按用户明确要求运行米家手动场景。"
            "仅对侧栏已加入白名单的场景生效；场景控制工具必须已由用户开启。"
        ),
        parameters=[
            ToolParameterInfo(
                name="stream_id",
                param_type=ToolParamType.STRING,
                description="当前聊天流 ID",
                required=True,
            ),
            ToolParameterInfo(
                name="scene",
                param_type=ToolParamType.STRING,
                description="场景名称",
                required=True,
            ),
        ],
    )
    async def tool_control_scene(
        self,
        stream_id: str,
        scene: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        del stream_id
        denial = await self._may_control(kwargs, scene=True)
        if denial:
            return {"success": True, "message": denial, "content": denial}
        try:
            settings = self._settings_copy()
            with self._state_lock:
                scenes = copy.deepcopy(self._snapshot["scenes"])
            selected = resolve_scene(scenes, scene)
            if not selected or not scene_is_allowed(
                settings,
                str(selected.get("scene_id")),
                str(selected.get("home_id")),
            ):
                return {
                    "success": True,
                    "message": REFUSAL_BUSY,
                    "content": REFUSAL_BUSY,
                }
            denial = self._control_denial(
                self._user_identity_from_context(kwargs),
                scene=True,
            )
            if denial:
                return {"success": True, "message": denial, "content": denial}
            if not await self._run_scene(selected):
                return {
                    "success": True,
                    "message": REFUSAL_FAILED,
                    "content": REFUSAL_FAILED,
                }
            success_message = f"好吧，{selected.get('name', '这个场景')} 已经启动了。"
            return {
                "success": True,
                "message": success_message,
                "content": success_message,
            }
        except Exception:
            self._log_exception("LLM 控制米家场景失败")
            return {
                "success": True,
                "message": REFUSAL_FAILED,
                "content": REFUSAL_FAILED,
            }

    @Command("mimai", description="查看或操作米家设备", pattern=r"^/mimai(?:\s|$)")
    async def command_mimai(self, **kwargs: Any) -> tuple[bool, str, int]:
        stream_id = str(kwargs.get("stream_id") or "")
        raw = str(kwargs.get("raw_message") or "")
        try:
            tokens = shlex.split(raw)
        except ValueError:
            tokens = []
        if len(tokens) < 2:
            reply = (
                "用法：/mimai 列表、/mimai 状态 设备名、/mimai 开 设备名、"
                "/mimai 关 设备名、/mimai 操作 设备名 动作名 [JSON参数]、"
                "/mimai 场景 场景名"
            )
            await self.ctx.send.text(reply, stream_id)
            return True, reply, 2

        action = tokens[1]
        target = " ".join(tokens[2:])
        if action == "列表":
            result = await self.tool_list_devices(stream_id)
            if not result.get("devices"):
                reply = result.get("message", REFUSAL_UNAVAILABLE)
            else:
                reply = "当前设备：" + "、".join(
                    item.get("alias") or item.get("name") or "未命名设备"
                    for item in result["devices"]
                )
            await self.ctx.send.text(reply, stream_id)
            return True, reply, 2

        if action == "状态":
            result = await self.tool_get_device_status(stream_id, target)
            if result.get("status"):
                lines = [
                    f"{item.get('description') or item.get('name')}: {item.get('value')}"
                    for item in result["status"]
                    if "value" in item
                ]
                reply = f"{result.get('device')}：\n" + "\n".join(lines) if lines else "这个设备暂时没读到状态。"
            else:
                reply = result.get("message", "这个设备暂时没读到状态。")
            await self.ctx.send.text(reply, stream_id)
            return True, reply, 2

        if action in {"开", "关"}:
            denial = await self._may_control(kwargs)
            if denial:
                await self.ctx.send.text(denial, stream_id)
                return True, denial, 2
            settings = self._settings_copy()
            with self._state_lock:
                devices = copy.deepcopy(self._snapshot["devices"])
            selected = resolve_device(devices, settings, target)
            if not selected or not device_is_allowed(settings, str(selected.get("did"))):
                reply = REFUSAL_BUSY
            else:
                try:
                    spec = self._get_spec(str(selected.get("spec_type") or ""))
                    property_names = ("on", "power", "switch")
                    prop = next(
                        (
                            item for item in spec["properties"]
                            if isinstance(item, dict)
                            and self._is_writable(item)
                            and (
                                str(item.get("name") or "").casefold() in property_names
                                or "开关" in str(item.get("description") or "")
                            )
                        ),
                        None,
                    )
                    if prop is None:
                        reply = "我没找到这个设备能直接开关的属性。"
                    else:
                        result = await self._set_property(
                            selected,
                            str(prop.get("name")),
                            "true" if action == "开" else "false",
                        )
                        reply = "好啦，已经给你打开了。" if result and action == "开" else (
                            "好啦，已经给你关掉了。" if result else REFUSAL_FAILED
                        )
                except Exception:
                    self._log_exception("命令控制米家设备失败")
                    reply = REFUSAL_FAILED
            await self.ctx.send.text(reply, stream_id)
            return True, reply, 2

        if action == "操作":
            denial = await self._may_control(kwargs)
            if denial:
                await self.ctx.send.text(denial, stream_id)
                return True, denial, 2
            if len(tokens) < 4:
                reply = "用法：/mimai 操作 设备别名 动作名 [JSON参数]"
            else:
                device_name = tokens[2]
                action_name = tokens[3]
                arguments = " ".join(tokens[4:]) or "[]"
                settings = self._settings_copy()
                with self._state_lock:
                    devices = copy.deepcopy(self._snapshot["devices"])
                selected = resolve_device(devices, settings, device_name)
                if not selected or not device_is_allowed(
                    settings,
                    str(selected.get("did")),
                ):
                    reply = REFUSAL_BUSY
                else:
                    try:
                        if await self._run_device_action(
                            selected,
                            action_name,
                            arguments,
                        ):
                            reply = f"好啦，{action_name}已经执行了。"
                        else:
                            reply = REFUSAL_FAILED
                    except ValueError:
                        reply = REFUSAL_NOT_IN_MOOD
                    except Exception:
                        self._log_exception("命令执行米家设备动作失败")
                        reply = REFUSAL_FAILED
            await self.ctx.send.text(reply, stream_id)
            return True, reply, 2

        if action == "场景":
            denial = await self._may_control(kwargs, scene=True)
            if denial:
                await self.ctx.send.text(denial, stream_id)
                return True, denial, 2
            settings = self._settings_copy()
            with self._state_lock:
                scenes = copy.deepcopy(self._snapshot["scenes"])
            selected_scene = resolve_scene(scenes, target)
            if not selected_scene or not scene_is_allowed(
                settings,
                str(selected_scene.get("scene_id")),
                str(selected_scene.get("home_id")),
            ):
                reply = REFUSAL_BUSY
            else:
                try:
                    if await self._run_scene(selected_scene):
                        reply = f"好吧，{selected_scene.get('name')} 已经启动了。"
                    else:
                        reply = REFUSAL_FAILED
                except Exception:
                    self._log_exception("命令控制米家场景失败")
                    reply = REFUSAL_FAILED
            await self.ctx.send.text(reply, stream_id)
            return True, reply, 2

        reply = "我没太听懂这条米家指令。"
        await self.ctx.send.text(reply, stream_id)
        return True, reply, 2


def create_plugin() -> MiMaiPlugin:
    return MiMaiPlugin()
