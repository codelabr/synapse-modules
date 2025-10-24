"""
Synapse Module: Auto Call Permissions
Tự động thiết lập quyền gọi thoại/video cho tất cả thành viên trong rooms

Cài đặt:
1. Đặt file này vào thư mục modules của Synapse
2. Cấu hình trong homeserver.yaml:
   
   modules:
     - module: call_permissions_module.CallPermissionsModule
       config:
         enable_auto_call_permissions: true
         call_permission_level: 0
         also_set_events_default: true

3. Restart Synapse
"""

import logging
from typing import Any, Dict, Optional, Tuple
from twisted.internet import defer, reactor
from synapse.module_api import ModuleApi
from synapse.events import EventBase
from synapse.api.constants import EventTypes, Membership
from synapse.types import UserID, create_requester

logger = logging.getLogger(__name__)


class CallPermissionsModule:
    """
    Module tự động cấu hình quyền gọi cho rooms
    """
    
    def __init__(self, config: Dict[str, Any], api: ModuleApi):
        """
        Khởi tạo module
        """
        self._api = api
        self._config = config
        self._reactor = reactor
        
        # Cấu hình
        self._enable_auto_call_permissions = config.get("enable_auto_call_permissions", True)
        self._call_permission_level = config.get("call_permission_level", 0)
        self._also_set_events_default = config.get("also_set_events_default", True)
        self._excluded_room_types = config.get("excluded_room_types", ["m.space"])
        
        logger.info("="*80)
        logger.info(f"🚀 CallPermissionsModule STARTED")
        logger.info(f"   Permission level: {self._call_permission_level}")
        logger.info(f"   Set events_default: {self._also_set_events_default}")
        logger.info("="*80)
        
        # Đăng ký callbacks
        self._api.register_third_party_rules_callbacks(
            check_event_allowed=self._check_event_allowed,
        )
    
    async def _check_event_allowed(
        self,
        event: EventBase,
        state_events: Dict[Any, Any],
    ) -> Tuple[bool, Optional[dict]]:
        """
        Callback kiểm tra event - trigger khi có room mới
        """
        logger.info(f"🔍 Processing event {event.type} for room {event.room_id}, is_direct: {event.content.get('is_direct', False)}")
        try:
            # Xử lý m.room.create events (room mới)
            if event.type == EventTypes.Create:
                if self._enable_auto_call_permissions:
                    room_id = event.room_id
                    
                    if not await self._should_exclude_room(event):
                        logger.info(f"🆕 NEW ROOM: {room_id} by {event.sender}")
                        
                        # Schedule với reactor.callLater
                        self._reactor.callLater(
                            3.0,  # delay 3 giây
                            lambda: defer.ensureDeferred(
                                self._setup_call_permissions_with_retry(room_id, event.sender, 0)
                            )
                        )
            
            return (True, None)
            
        except Exception as e:
            logger.error(f"❌ Error in check_event_allowed: {e}", exc_info=True)
            return (True, None)
    
    async def _setup_call_permissions_with_retry(
        self, 
        room_id: str, 
        creator: str,
        attempt: int
    ) -> None:
        """
        Setup permissions với retry logic - không dùng asyncio.sleep
        """
        max_attempts = 6
        
        try:
            logger.info(f"⏳ Attempt {attempt + 1}/{max_attempts} for room {room_id}")
            
            # Kiểm tra xem room đã sẵn sàng chưa
            state = await self._api.get_room_state(room_id)
            if state and (EventTypes.PowerLevels, "") in state:
                logger.info(f"✅ Room {room_id} is ready")
                await self._setup_call_permissions(room_id, creator)
                return
            
            # Chưa sẵn sàng, retry
            if attempt < max_attempts - 1:
                wait_time = 2 ** attempt  # exponential backoff
                logger.debug(f"Room not ready, retrying in {wait_time}s...")
                
                # Schedule retry với reactor.callLater
                self._reactor.callLater(
                    wait_time,
                    lambda: defer.ensureDeferred(
                        self._setup_call_permissions_with_retry(room_id, creator, attempt + 1)
                    )
                )
            else:
                logger.error(f"❌ Room {room_id} never became ready after {max_attempts} attempts")
            
        except Exception as e:
            logger.error(f"❌ Error in retry setup for {room_id}: {e}", exc_info=True)
    
    async def _should_exclude_room(self, create_event: EventBase) -> bool:
        """
        Kiểm tra room có nên bị loại trừ không
        """
        room_type = create_event.content.get("type")
        if room_type in self._excluded_room_types:
            logger.info(f"⏭️  Excluding room type: {room_type}")
            return True
        # Kiểm tra nếu là DM
        is_direct = create_event.content.get("is_direct", False)
        if is_direct:
            logger.info(f"🎯 Processing DM room")
            return False            
        return False
    
    async def _setup_call_permissions(self, room_id: str, sender: str) -> None:
        """
        Thiết lập quyền gọi cho room
        """
        try:
            logger.info(f"🔧 Setting up call permissions for room {room_id}")
            
            # Lấy power levels hiện tại
            state = await self._api.get_room_state(room_id)
            power_levels_event = state.get((EventTypes.PowerLevels, ""))
            
            if not power_levels_event:
                logger.error(f"❌ No power levels found for {room_id}")
                return
            
            # Clone content - DEEP COPY để tránh immutabledict
            import copy
            new_power_levels = copy.deepcopy(dict(power_levels_event.content))
            
            # Đảm bảo events là dict thông thường
            if "events" not in new_power_levels:
                new_power_levels["events"] = {}
            else:
                # Convert immutabledict thành dict thông thường
                new_power_levels["events"] = dict(new_power_levels["events"])
            
            events = new_power_levels["events"]
            
            logger.info(f"📊 Current events_default: {new_power_levels.get('events_default', 0)}")
            
            # Danh sách FULL các call events
            call_events = [
                # 1:1 calls
                "m.call.invite",
                "m.call.answer",
                "m.call.hangup",
                "m.call.candidates",
                "m.call.select_answer",
                "m.call.reject",
                "m.call.negotiate",
                # Group calls
                "org.matrix.msc3401.call",
                "org.matrix.msc3401.call.member",
                "m.call.member",
                # Widgets
                "im.vector.modular.widgets",
            ]
            
            # Update events - Tạo dict mới thay vì modify
            changes = []
            updated_events = {}
            
            # Copy tất cả events hiện có
            for k, v in events.items():
                updated_events[k] = v
            
            # Update call events
            for event_type in call_events:
                old_level = updated_events.get(event_type, "not set")
                updated_events[event_type] = self._call_permission_level
                if old_level != self._call_permission_level:
                    changes.append(f"{event_type}: {old_level} → {self._call_permission_level}")
            
            # Gán lại events dict mới
            new_power_levels["events"] = updated_events
            
            # CRITICAL: Set events_default
            if self._also_set_events_default:
                old_default = new_power_levels.get("events_default", 0)
                if old_default > self._call_permission_level:
                    new_power_levels["events_default"] = self._call_permission_level
                    changes.append(f"events_default: {old_default} → {self._call_permission_level}")
            
            if not changes:
                logger.info(f"✅ Room {room_id} already correct")
                return
            
            logger.info(f"🔄 Applying {len(changes)} changes")
            for change in changes[:5]:  # Log first 5
                logger.info(f"   • {change}")
            
            # Tìm admin để send event
            admin_user = await self._find_admin_user(new_power_levels)
            if not admin_user:
                admin_user = sender
            
            logger.info(f"👤 Using user: {admin_user}")
            
            # Send state event
            success = await self._send_state_event(
                room_id=room_id,
                event_type=EventTypes.PowerLevels,
                content=new_power_levels,
                state_key="",
                user_id=admin_user
            )
            
            if success:
                logger.info(f"✅ SUCCESS for room {room_id}")
                # Schedule verification sau 1 giây
                self._reactor.callLater(
                    1.0,
                    lambda: defer.ensureDeferred(self._verify_permissions(room_id))
                )
            else:
                logger.error(f"❌ FAILED for room {room_id}")
                
        except Exception as e:
            logger.error(f"❌ Error setting up {room_id}: {e}", exc_info=True)
    
    async def _send_state_event(
        self,
        room_id: str,
        event_type: str,
        content: Dict[str, Any],
        state_key: str,
        user_id: str
    ) -> bool:
        """
        Gửi state event
        """
        try:
            # Tạo requester
            requester = create_requester(
                user_id=user_id,
                authenticated_entity=user_id,
            )
            
            # Lấy event creation handler
            event_creation_handler = self._api._hs.get_event_creation_handler()
            
            # Tạo và gửi event
            event, _ = await event_creation_handler.create_and_send_nonmember_event(
                requester=requester,
                event_dict={
                    "type": event_type,
                    "room_id": room_id,
                    "sender": user_id,
                    "state_key": state_key,
                    "content": content,
                },
                ratelimit=False,
            )
            
            logger.info(f"✅ Event sent: {event.event_id}")
            return True
            
        except Exception as e:
            logger.error(f"❌ Error sending state event: {e}", exc_info=True)
            
            # Fallback
            try:
                logger.info("🔄 Trying fallback method...")
                event_dict = {
                    "type": event_type,
                    "room_id": room_id,
                    "sender": user_id,
                    "state_key": state_key,
                    "content": content,
                }
                event, _ = await self._api.create_and_send_event_into_room(event_dict)
                logger.info(f"✅ Fallback success: {event.event_id}")
                return True
            except Exception as e2:
                logger.error(f"❌ Fallback failed: {e2}")
                return False
    
    async def _verify_permissions(self, room_id: str) -> None:
        """
        Verify permissions sau khi update
        """
        try:
            state = await self._api.get_room_state(room_id)
            pl_event = state.get((EventTypes.PowerLevels, ""))
            
            if not pl_event:
                return
            
            events = pl_event.content.get("events", {})
            events_default = pl_event.content.get("events_default", 0)
            
            logger.info(f"🔍 Verification for {room_id}:")
            logger.info(f"   events_default: {events_default}")
            
            checks = ["m.call.invite", "m.call.member", "org.matrix.msc3401.call.member"]
            for evt in checks:
                level = events.get(evt, events_default)
                status = "✅" if level == self._call_permission_level else "❌"
                logger.info(f"   {status} {evt}: {level}")
                
        except Exception as e:
            logger.error(f"Error verifying: {e}")
    
    async def _find_admin_user(self, power_levels: Dict[str, Any]) -> Optional[str]:
        """
        Tìm admin user
        """
        users = power_levels.get("users", {})
        
        for user_id, power in users.items():
            if power >= 50:
                return user_id
        
        return None
    
    @staticmethod
    def parse_config(config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Parse config
        """
        return {
            "enable_auto_call_permissions": config.get("enable_auto_call_permissions", True),
            "call_permission_level": config.get("call_permission_level", 0),
            "also_set_events_default": config.get("also_set_events_default", True),
            "excluded_room_types": config.get("excluded_room_types", ["m.space"]),
        }


def create_module(config: Dict[str, Any], api: ModuleApi) -> CallPermissionsModule:
    """
    Factory function
    """
    parsed_config = CallPermissionsModule.parse_config(config)
    return CallPermissionsModule(parsed_config, api)
