"""
Synapse Module: Auto Call Permissions
Tự động thiết lập quyền gọi thoại/video cho tất cả thành viên trong rooms mới

Cài đặt:
1. Đặt file này vào thư mục modules của Synapse
2. Cấu hình trong homeserver.yaml
3. Restart Synapse
"""

import logging
from typing import Any, Dict, Optional, Tuple, Union
from synapse.module_api import ModuleApi
from synapse.types import Requester, StateMap
from synapse.events import EventBase
from synapse.api.constants import EventTypes, Membership

logger = logging.getLogger(__name__)


class CallPermissionsModule:
    """
    Module tự động cấu hình quyền gọi cho rooms mới
    """
    
    def __init__(self, config: Dict[str, Any], api: ModuleApi):
        """
        Khởi tạo module
        
        Args:
            config: Cấu hình từ homeserver.yaml
            api: Module API của Synapse
        """
        self._api = api
        self._config = config
        
        # Cấu hình mặc định
        self._enable_auto_call_permissions = config.get("enable_auto_call_permissions", True)
        self._call_permission_level = config.get("call_permission_level", 0)  # 0 = tất cả users
        self._excluded_room_types = config.get("excluded_room_types", [])
        self._excluded_room_prefixes = config.get("excluded_room_prefixes", ["#admin:", "#system:"])
        
        # Log cấu hình
        logger.info(f"CallPermissionsModule loaded with config: {config}")
        
        # Đăng ký callbacks
        self._api.register_third_party_rules_callbacks(
            on_new_event=self._on_new_event,
        )
        
        # Đăng ký callback cho room creation (nếu có)
        try:
            self._api.register_spam_checker_callbacks(
                check_event_for_spam=self._check_event_for_spam,
            )
        except AttributeError:
            # Fallback cho versions cũ hơn
            pass
    
    async def _on_new_event(
        self,
        event: EventBase,
        state_events: StateMap[EventBase],
    ) -> None:
        """
        Callback khi có event mới
        Kiểm tra nếu là room creation event thì thiết lập quyền gọi
        """
        try:
            # Chỉ xử lý m.room.create events
            if event.type != EventTypes.Create:
                return
                
            # Bỏ qua nếu tính năng bị tắt
            if not self._enable_auto_call_permissions:
                return
                
            room_id = event.room_id
            
            # Kiểm tra loại room có bị loại trừ không
            if await self._should_exclude_room(room_id, event):
                logger.info(f"Skipping room {room_id} - excluded by configuration")
                return
            
            logger.info(f"Setting up call permissions for new room: {room_id}")
            
            # Đợi một chút để room được tạo hoàn toàn
            await self._setup_call_permissions(room_id)
            
        except Exception as e:
            logger.error(f"Error in _on_new_event: {e}", exc_info=True)
    
    async def _check_event_for_spam(
        self,
        event: EventBase,
    ) -> Union[bool, str]:
        """
        Spam checker callback - sử dụng để detect room creation
        """
        try:
            if event.type == EventTypes.Create:
                # Không phải spam, nhưng trigger setup permissions
                room_id = event.room_id
                if not await self._should_exclude_room(room_id, event):
                    # Schedule permission setup
                    self._api.run_in_background(self._setup_call_permissions, room_id)
        except Exception as e:
            logger.error(f"Error in spam checker: {e}", exc_info=True)
        
        # Không block event
        return False
    
    async def _should_exclude_room(self, room_id: str, create_event: EventBase) -> bool:
        """
        Kiểm tra room có nên bị loại trừ không
        """
        try:
            # Kiểm tra theo room type
            room_type = create_event.content.get("type")
            if room_type in self._excluded_room_types:
                return True
            
            # Kiểm tra theo prefix của room alias
            try:
                aliases = await self._api.get_room_aliases(room_id)
                for alias in aliases:
                    for prefix in self._excluded_room_prefixes:
                        if alias.startswith(prefix):
                            return True
            except:
                pass  # Không có aliases hoặc lỗi
            
            # Kiểm tra room name
            try:
                room_name = create_event.content.get("name", "")
                for prefix in ["Admin", "System", "Bot"]:
                    if room_name.startswith(prefix):
                        return True
            except:
                pass
                
            return False
            
        except Exception as e:
            logger.error(f"Error checking room exclusion: {e}")
            return False
    
    async def _setup_call_permissions(self, room_id: str) -> None:
        """
        Thiết lập quyền gọi cho room
        """
        try:
            # Đợi room được tạo hoàn toàn
            await self._api.sleep(2)
            
            # Lấy power levels hiện tại
            current_power_levels = await self._get_room_power_levels(room_id)
            if not current_power_levels:
                logger.warning(f"Could not get power levels for room {room_id}")
                return
            
            # Thiết lập quyền gọi
            events = current_power_levels.setdefault("events", {})
            
            # Các events liên quan đến calls
            call_events = [
                "m.call.invite",
                "m.call.answer", 
                "m.call.hangup",
                "m.call.select_answer",
                "org.matrix.msc3401.call.member",  # Group calls
                "org.matrix.msc3401.call",
            ]
            
            # Kiểm tra xem có cần cập nhật không
            needs_update = False
            for event_type in call_events:
                if events.get(event_type, 50) != self._call_permission_level:
                    events[event_type] = self._call_permission_level
                    needs_update = True
            
            if not needs_update:
                logger.info(f"Room {room_id} already has correct call permissions")
                return
            
            # Cập nhật power levels
            success = await self._update_room_power_levels(room_id, current_power_levels)
            
            if success:
                logger.info(f"✅ Successfully set call permissions for room {room_id}")
            else:
                logger.error(f"❌ Failed to set call permissions for room {room_id}")
                
        except Exception as e:
            logger.error(f"Error setting up call permissions for {room_id}: {e}", exc_info=True)
    
    async def _get_room_power_levels(self, room_id: str) -> Optional[Dict[str, Any]]:
        """
        Lấy power levels hiện tại của room
        """
        try:
            state = await self._api.get_room_state(room_id)
            power_levels_event = state.get((EventTypes.PowerLevels, ""))
            
            if power_levels_event:
                return dict(power_levels_event.content)
            else:
                # Tạo power levels mặc định
                return {
                    "users": {},
                    "users_default": 0,
                    "events": {},
                    "events_default": 0,
                    "state_default": 50,
                    "ban": 50,
                    "kick": 50,
                    "redact": 50,
                    "invite": 0,
                }
        except Exception as e:
            logger.error(f"Error getting power levels for {room_id}: {e}")
            return None
    
    async def _update_room_power_levels(self, room_id: str, power_levels: Dict[str, Any]) -> bool:
        """
        Cập nhật power levels cho room
        """
        try:
            # Tìm một admin user để thực hiện cập nhật
            admin_user = await self._find_admin_user(room_id)
            if not admin_user:
                logger.error(f"No admin user found for room {room_id}")
                return False
            
            # Tạo requester
            requester = Requester.test()  # Hoặc tạo requester từ admin user
            
            # Send state event
            await self._api.create_and_send_event_into_room(
                {
                    "type": EventTypes.PowerLevels,
                    "room_id": room_id,
                    "sender": admin_user,
                    "content": power_levels,
                    "state_key": "",
                }
            )
            
            return True
            
        except Exception as e:
            logger.error(f"Error updating power levels for {room_id}: {e}")
            return False
    
    async def _find_admin_user(self, room_id: str) -> Optional[str]:
        """
        Tìm một admin user trong room
        """
        try:
            # Lấy danh sách members
            members = await self._api.get_room_members(room_id)
            
            # Lấy power levels
            power_levels = await self._get_room_power_levels(room_id)
            if not power_levels:
                return None
            
            users = power_levels.get("users", {})
            
            # Tìm user có power level cao nhất
            max_power = 0
            admin_user = None
            
            for user_id in members:
                user_power = users.get(user_id, power_levels.get("users_default", 0))
                if user_power >= max_power and user_power >= 50:  # Admin level
                    max_power = user_power
                    admin_user = user_id
            
            return admin_user
            
        except Exception as e:
            logger.error(f"Error finding admin user for {room_id}: {e}")
            return None
    
    @staticmethod
    def parse_config(config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Parse và validate config
        """
        return {
            "enable_auto_call_permissions": config.get("enable_auto_call_permissions", True),
            "call_permission_level": config.get("call_permission_level", 0),
            "excluded_room_types": config.get("excluded_room_types", []),
            "excluded_room_prefixes": config.get("excluded_room_prefixes", ["#admin:", "#system:"]),
        }


# Entry point cho Synapse
def create_module(config: Dict[str, Any], api: ModuleApi) -> CallPermissionsModule:
    """
    Factory function để tạo module instance
    """
    return CallPermissionsModule(config, api)